"""Immutable WAV exports, atomically published with their input manifest."""

import json
import os
import shutil
import wave
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import Field, StrictBool

from .generation import GenerationRegistry, TaskError
from .references import require_project, timestamp
from .storage import connect, pending_deletion_ids
from .text_models import Revision, StrictModel
from .versions import audio_metadata, catalogue, require_audio, value_digest


class ExportInput(StrictModel):
    request_id: UUID
    adapter_id: str = Field(min_length=1, max_length=100)
    comparison_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    selections: dict[
        UUID, Revision
    ]  # Complete displayed selection revision set, including single export.
    paragraph_id: UUID | None = None
    pause_ms: int = Field(default=0, ge=0, le=2000, strict=True)
    acknowledge_mismatch: StrictBool = False


def export_root(directory: Path, project: str) -> Path:
    root = directory / "exports"
    path = root / str(UUID(project))
    if root.is_symlink() or path.is_symlink():
        raise TaskError(409, "export_unavailable", "导出目录不可用")
    return path


def read_export(folder: Path, project: str) -> dict[str, Any]:
    if folder.is_symlink() or (folder / "manifest.json").is_symlink():
        raise ValueError("linked export")
    data: dict[str, Any] = json.loads((folder / "manifest.json").read_text())
    if data["id"] != folder.name or data["project_id"] != project or data["schema"] != 1:
        raise ValueError("invalid export identity")
    checksum = data.pop("content_sha256")
    if checksum != value_digest(data):
        raise ValueError("changed manifest")
    data["content_sha256"] = checksum
    if not isinstance(data["segments"], list) or not data["segments"]:
        raise ValueError("invalid segments")
    offset = 0
    for index, segment in enumerate(data["segments"]):
        if index:
            offset += data["pause_ms"] * 24
        if (
            segment["start_frame"] != offset
            or segment["frame_count"] != segment["version"]["frame_count"]
            or segment["version"]["comparison"] not in {"matching", "stale", "unknown"}
            or segment["selection"]["version_id"] != segment["version"]["id"]
        ):
            raise ValueError("invalid segment provenance")
        offset += segment["frame_count"]
    if offset != data["audio"]["frame_count"]:
        raise ValueError("invalid total frames")
    return data


def verified_audio(folder: Path, manifest: dict[str, Any]) -> bytes:
    actual = audio_metadata(folder / "audio.wav")
    if actual != manifest["audio"]:
        raise ValueError("changed export")
    raw = (folder / "audio.wav").read_bytes()
    import hashlib

    if hashlib.sha256(raw).hexdigest() != actual["sha256"]:
        raise ValueError("changed while reading")
    return raw


def cleanup_exports(directory: Path, database: Path) -> None:
    root = directory / "exports"
    if not root.exists() or root.is_symlink():
        return
    pending_projects = pending_deletion_ids(database, "exports")
    for project in root.iterdir():
        if project.name in pending_projects or project.is_symlink() or not project.is_dir():
            continue
        for pending in project.glob(".pending-*"):
            if pending.is_dir() and not pending.is_symlink():
                shutil.rmtree(pending)


def export_router(
    database: Path,
    directory: Path,
    require_storage: Callable[[], None],
    registry: GenerationRegistry,
) -> APIRouter:
    routes = APIRouter(prefix="/api/projects/{project_id}/exports")
    export_lock = Lock()

    @routes.get("")
    def history(project_id: UUID) -> list[dict[str, Any]]:
        require_storage()
        project = str(project_id)
        with connect(database) as connection:
            require_project(connection, project)
        root = export_root(directory, project)
        result = []
        if root.exists():
            for folder in sorted(root.iterdir()):
                try:
                    UUID(folder.name)
                except ValueError:
                    continue
                try:
                    manifest = read_export(folder, project)
                    verified_audio(folder, manifest)
                    result.append({**manifest, "available": True})
                except (OSError, ValueError, KeyError, TypeError, TaskError):
                    result.append({"id": folder.name, "available": False})
        return result

    @routes.post("")
    def create(project_id: UUID, payload: ExportInput) -> dict[str, Any]:
        require_storage()
        project, identifier = str(project_id), str(payload.request_id)
        fingerprint = value_digest(payload.model_dump(mode="json"))
        root = export_root(directory, project)
        final = root / identifier
        pending = root / f".pending-{uuid4()}"
        export_lock.acquire()
        try:
            with connect(database) as connection:
                require_project(connection, project)
            if final.exists():
                old = read_export(final, project)
                if old["request_sha256"] != fingerprint:
                    raise TaskError(409, "export_conflict", "同一导出请求内容不同，请重新核对")
                verified_audio(final, old)
                return {**old, "available": True}
            with connect(database) as connection:
                # Freeze a consistent database snapshot; release before writing audio.
                connection.execute("BEGIN")
                require_project(connection, project)
                catalog = catalogue(
                    connection, directory, project, registry, payload.adapter_id, verify_files=False
                )
                expected = {str(k): v for k, v in payload.selections.items()}
                actual = {
                    p["id"]: p["selection"]["revision"] if p["selection"] else 0
                    for p in catalog["paragraphs"]
                }
                if catalog["comparison_token"] != payload.comparison_token or expected != actual:
                    raise TaskError(
                        409, "export_inputs_changed", "文案或选用已改变，请刷新版本后重新核对"
                    )
                if not catalog["confirmation_current"]:
                    raise TaskError(409, "export_unconfirmed", "请先确认已保存文案，再导出")
                paragraphs = [
                    p
                    for p in catalog["paragraphs"]
                    if payload.paragraph_id is None or p["id"] == str(payload.paragraph_id)
                ]
                if not paragraphs:
                    raise TaskError(409, "export_empty", "没有可导出的段落")
                selected = []
                for p in paragraphs:
                    version = next(
                        (
                            v
                            for v in p["versions"]
                            if p["selection"] and v["id"] == p["selection"]["version_id"]
                        ),
                        None,
                    )
                    if not version or not version["available"]:
                        raise TaskError(409, "export_missing", "段落缺少可用选用音频，请先补齐")
                    if version["comparison"] != "matching" and not payload.acknowledge_mismatch:
                        raise TaskError(
                            409, "export_acknowledgement", "请明确确认导出过时或无法核定的音频"
                        )
                    selected.append((p, version))
                frames = sum(v["frame_count"] for _, v in selected)
                frames += (len(selected) - 1) * payload.pause_ms * 24
                if frames > 24000 * 600:
                    raise TaskError(422, "export_too_long", "整篇含新增停顿最多支持10分钟")
            pending.mkdir(parents=True)
            segments = []
            offset = 0
            with wave.open(str(pending / "audio.wav"), "wb") as output:
                output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                for index, (p, v) in enumerate(selected):
                    try:
                        path = require_audio(directory, v)
                    except (OSError, TaskError, ValueError, KeyError) as exc:
                        raise TaskError(409, "export_missing", "段落音频不可用，请核对") from exc
                    raw = path.read_bytes()
                    import hashlib

                    if hashlib.sha256(raw).hexdigest() != v["sha256"]:
                        raise TaskError(409, "export_source_changed", "源音频已改变，请核对")
                    with wave.open(BytesIO(raw), "rb") as source:
                        pcm = source.readframes(source.getnframes())
                    if index:
                        silence = b"\x00\x00" * (payload.pause_ms * 24)
                        output.writeframesraw(silence)
                        offset += len(silence) // 2
                    segments.append(
                        {
                            "paragraph_id": p["id"],
                            "current_draft_text": p["draft_text"],
                            "generated_draft_text": v["dependencies"]["draft_text"],
                            "selection": p["selection"],
                            "version": v,
                            "start_frame": offset,
                            "frame_count": len(pcm) // 2,
                        }
                    )
                    output.writeframesraw(pcm)
                    offset += len(pcm) // 2
            manifest = {
                "schema": 1,
                "id": identifier,
                "project_id": project,
                "created_at": timestamp(),
                "request_sha256": fingerprint,
                "document_revision": catalog["document_revision"],
                "comparison_token": catalog["comparison_token"],
                "pause_ms": payload.pause_ms,
                "segments": segments,
                "is_simulated": any(v["is_simulated"] for _, v in selected),
                "acknowledged_mismatch": payload.acknowledge_mismatch,
                "audio": audio_metadata(pending / "audio.wav"),
            }
            manifest["content_sha256"] = value_digest(manifest)
            (pending / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2)
            )
            for name in ("audio.wav", "manifest.json"):
                with (pending / name).open("rb") as file:
                    os.fsync(file.fileno())
            os.replace(pending, final)
            return {**manifest, "available": True}
        except TaskError:
            raise
        except (OSError, ValueError, KeyError, TypeError, wave.Error) as exc:
            raise TaskError(
                409, "export_failed", "导出未完成或文件不可用，请刷新导出历史核对；旧版本保留"
            ) from exc
        finally:
            try:
                if pending.exists():
                    shutil.rmtree(pending)
            finally:
                export_lock.release()

    @routes.get("/{export_id}/{kind}")
    def download(project_id: UUID, export_id: UUID, kind: str) -> Response:
        require_storage()
        project = str(project_id)
        with connect(database) as connection:
            require_project(connection, project)
        folder = export_root(directory, project) / str(export_id)
        try:
            manifest = read_export(folder, project)
            raw = verified_audio(folder, manifest)
        except (OSError, ValueError, KeyError, TypeError, TaskError) as exc:
            raise TaskError(
                409, "export_unavailable", "导出文件不可用，请核对本地文件；历史保留"
            ) from exc
        if kind not in {"audio", "manifest"}:
            raise TaskError(404, "not_found", "下载类型不存在")
        prefix = "simulated-" if manifest["is_simulated"] else "voice-"
        name = f"{prefix}{export_id}.{'wav' if kind == 'audio' else 'json'}"
        content = raw if kind == "audio" else json.dumps(manifest, ensure_ascii=False).encode()
        return Response(
            content,
            media_type="audio/wav" if kind == "audio" else "application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return routes
