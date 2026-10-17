"""Validate actual paragraph boundaries before publishing an entire task atomically."""

import json
import wave
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .generation import TaskError
from .tasks import canonical
from .versions import audio_metadata, dependencies, value_digest


def write_manifest(stage: Path, paragraph_ids: list[str]) -> None:
    segments = []
    for paragraph_id in paragraph_ids:
        filename = f"segments/{UUID(paragraph_id)}.wav"
        segments.append(
            {
                "paragraph_id": paragraph_id,
                "file": filename,
                **audio_metadata(stage / filename),
            }
        )
    (stage / "segments.json").write_text(canonical({"schema": 1, "segments": segments}))


def collect_segments(stage: Path, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        manifest = json.loads((stage / "segments.json").read_text())
        items = manifest["segments"]
        ids = snapshot["paragraph_ids"]
        if (
            manifest["schema"] != 1
            or not isinstance(items, list)
            or [item["paragraph_id"] for item in items] != ids
            or len(set(ids)) != len(ids)
            or (stage / "segments").is_symlink()
        ):
            raise ValueError("segment identity mismatch")
        result = []
        with wave.open(str(stage / "audio.wav"), "rb") as combined:
            for ordinal, item in enumerate(items, start=1):
                paragraph_id = str(UUID(item["paragraph_id"]))
                expected_file = f"segments/{paragraph_id}.wav"
                if item["file"] != expected_file:
                    raise ValueError("segment path mismatch")
                path = stage / expected_file
                metadata = audio_metadata(path)
                if any(item.get(key) != value for key, value in metadata.items()):
                    raise ValueError("segment metadata mismatch")
                with wave.open(str(path), "rb") as part:
                    pcm = part.readframes(part.getnframes())
                    if pcm != combined.readframes(part.getnframes()):
                        raise ValueError("segment PCM mismatch")
                dependency = dependencies(snapshot, paragraph_id)
                result.append(
                    {
                        **metadata,
                        "id": str(uuid4()),
                        "paragraph_id": paragraph_id,
                        "ordinal": ordinal,
                        "file": expected_file,
                        "dependencies": dependency,
                        "dependency_sha256": value_digest(dependency),
                    }
                )
            if combined.readframes(1):
                raise ValueError("combined audio has unclaimed frames")
        return result
    except (OSError, ValueError, KeyError, TypeError, wave.Error, EOFError) as exc:
        raise TaskError(
            502,
            "adapter_invalid_output",
            "段落文件或清单不完整，未发布本次结果",
        ) from exc
