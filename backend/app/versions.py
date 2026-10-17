"""Read-only audio history with explicit dependency comparison and CAS selection."""

import hashlib
import sqlite3
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from pydantic import Field, StrictBool
from starlette.exceptions import HTTPException

from .generation import GenerationRegistry, TaskError
from .references import require_project, timestamp
from .storage import connect
from .tasks import canonical, file_digest, task_from_row, validate_clip
from .text_documents import load_document
from .text_models import Revision, StrictModel
from .transcripts import transcript_value

CONTRACT = "paragraphs-v1"
IDENTITY_KEYS = (
    "id",
    "version",
    "model_id",
    "model_revision",
    "model_fingerprint",
    "config_fingerprint",
    "environment_fingerprint",
)


def value_digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def audio_metadata(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise TaskError(502, "adapter_invalid_output", "生成音频不能是链接文件")
    try:
        with wave.open(str(path), "rb") as audio:
            rate, channels, width, frames = (
                audio.getframerate(),
                audio.getnchannels(),
                audio.getsampwidth(),
                audio.getnframes(),
            )
            if (
                rate != 24000
                or channels != 1
                or width != 2
                or not 1 <= frames <= rate * 600
                or len(audio.readframes(frames)) != frames * width * channels
            ):
                raise TaskError(502, "adapter_invalid_output", "生成音频不是完整的24k单声道PCM")
    except (wave.Error, EOFError) as exc:
        raise TaskError(502, "adapter_invalid_output", "生成音频WAV无效") from exc
    return {
        "media_type": "audio/wav",
        "byte_size": path.stat().st_size,
        "sha256": file_digest(path),
        "duration_ms": frames * 1000 // rate,
        "sample_rate": rate,
        "channels": channels,
        "sample_width": width,
        "frame_count": frames,
    }


def dependencies(snapshot: dict[str, Any], paragraph_id: str) -> dict[str, Any]:
    """Semantic input relationship; source ordinal/RNG provenance stays in the task."""
    adapter = snapshot["adapter"]
    paragraphs = snapshot["confirmation"]["paragraphs"]
    paragraph = next(item for item in paragraphs if item["id"] == paragraph_id)
    reference = snapshot["reference"]
    simulated = snapshot.get("is_simulated", True)
    transcript = reference.get("transcript", {})
    known = (
        adapter["id"] in {"simulated-tone", "cosyvoice3-local"}
        and adapter["version"] == "1"
        and snapshot.get("dependency_known", True)
    )
    return {
        "schema": 1,
        "known": known,
        "paragraph_id": paragraph_id,
        "draft_text": paragraph["draft_text"],
        "is_simulated": simulated,
        "reference": {"clip_id": reference["clip"]["id"], "sha256": reference["clip_sha256"]},
        "transcript": None if simulated else transcript.get("text"),
        "adapter": {key: adapter.get(key) for key in IDENTITY_KEYS},
        "parameters": snapshot["parameters"],
    }


def candidates(connection: sqlite3.Connection, project_id: str) -> tuple[list[dict[str, Any]], int]:
    rows = connection.execute(
        "SELECT * FROM generation_tasks WHERE project_id=? AND state='succeeded' "
        "ORDER BY created_at,id",
        (project_id,),
    )
    versions: list[dict[str, Any]] = []
    legacy_full = 0
    for row in rows:
        task = task_from_row(row)
        snapshot, result = task["snapshot"], task["result"]
        if not result:
            continue
        if snapshot.get("output_contract") == CONTRACT:
            segments = result.get("segments", [])
        elif snapshot["mode"] == "preview" and len(snapshot["paragraph_ids"]) == 1:
            paragraph_id = snapshot["paragraph_ids"][0]
            segments = [
                {
                    "id": str(uuid5(NAMESPACE_URL, f"voice-style-web:{task['id']}:{paragraph_id}")),
                    "paragraph_id": paragraph_id,
                    "ordinal": 1,
                    "file": "audio.wav",
                    "dependencies": dependencies(snapshot, paragraph_id),
                    **result,
                }
            ]
        else:
            legacy_full += 1
            continue
        for segment in segments:
            versions.append(
                {
                    **segment,
                    "task_id": task["id"],
                    "created_at": task["created_at"],
                    "mode": snapshot["mode"],
                    "is_simulated": task["is_simulated"],
                    "adapter": snapshot["adapter"],
                    "snapshot_sha256": task["snapshot_sha256"],
                    "legacy": snapshot.get("output_contract") != CONTRACT,
                }
            )
    return versions, legacy_full


def version_path(directory: Path, version: dict[str, Any]) -> Path:
    task_id, paragraph_id = str(UUID(version["task_id"])), str(UUID(version["paragraph_id"]))
    expected = "audio.wav" if version["legacy"] else f"segments/{paragraph_id}.wav"
    if version["file"] != expected:
        raise TaskError(409, "version_unavailable", "段落音频路径不符合保存契约")
    return directory / "generation-results" / task_id / expected


def require_audio(directory: Path, version: dict[str, Any]) -> Path:
    path = version_path(directory, version)
    actual = audio_metadata(path)
    if any(actual[key] != version[key] for key in ("sha256", "byte_size", "frame_count")):
        raise TaskError(409, "version_unavailable", "段落音频缺失或内容已改变")
    return path


def current_inputs(
    connection: sqlite3.Connection,
    directory: Path,
    project_id: str,
    registry: GenerationRegistry,
    adapter_id: str,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    document = load_document(connection, project_id)
    adapter = registry.require(adapter_id).capability.model_dump(mode="json")
    readiness: list[str] = []
    if not document["confirmation_current"]:
        readiness.append("已保存文案尚未人工确认；不能按当前稿生成")
    health = registry.health(adapter_id)
    if not health["execution_available"]:
        readiness.append("当前生成组件不可调用；历史音频仍保留")
    row = connection.execute(
        "SELECT c.* FROM project_reference_selection s "
        "JOIN reference_clips c ON c.id=s.clip_id WHERE s.project_id=?",
        (project_id,),
    ).fetchone()
    reference: dict[str, Any] = {"clip": {"id": None}, "clip_sha256": None}
    if row is None:
        readiness.append("未选择当前参考片段")
    else:
        reference["clip"] = dict(row)
        path = directory / "references" / row["reference_id"] / "clips" / f"{row['id']}.wav"
        try:
            reference["clip_sha256"] = validate_clip(path, row["start_ms"], row["end_ms"])
            if not adapter["is_simulated"]:
                reference["transcript"] = transcript_value(
                    connection,
                    row["id"],
                    reference["clip_sha256"],
                )
                if not reference["transcript"]["text"].strip():
                    readiness.append("当前参考片段缺少准确原话")
        except (OSError, TaskError):
            readiness.append("当前参考或绑定原话不可核定，请核对素材")
    snapshot = {
        "confirmation": document,
        "adapter": adapter,
        "reference": reference,
        "parameters": {},
        "is_simulated": adapter["is_simulated"],
        "dependency_known": health["configuration_valid"],
    }
    return document, snapshot, readiness


def compare(old: dict[str, Any], current: dict[str, Any]) -> tuple[list[str], bool]:
    reasons = []
    for key, message in (
        ("draft_text", "本段草稿已改变"),
        ("parameters", "生成参数已改变"),
        ("is_simulated", "真实/模拟组件已改变"),
    ):
        if old.get(key) != current.get(key):
            reasons.append(message)
    # Missing model fingerprints mean unknown identity, not evidence of a change.
    old_adapter, new_adapter = old.get("adapter", {}), current["adapter"]
    if any(
        old_adapter.get(key) is not None
        and new_adapter.get(key) is not None
        and old_adapter[key] != new_adapter[key]
        for key in IDENTITY_KEYS
    ):
        reasons.append("生成组件、模型或配置已改变")
    old_ref, new_ref = old.get("reference", {}), current["reference"]
    if new_ref["clip_id"] is not None and old_ref.get("clip_id") != new_ref["clip_id"]:
        reasons.append("当前参考片段已改变")
    elif new_ref["sha256"] is not None and old_ref.get("sha256") != new_ref["sha256"]:
        reasons.append("参考音频内容已改变")
    if current["transcript"] is not None and old.get("transcript") != current["transcript"]:
        reasons.append("参考准确原话已改变")
    known = bool(
        old.get("known")
        and current["known"]
        and new_ref["sha256"]
        and (current["is_simulated"] or current["transcript"])
    )
    return reasons, known


def catalogue(
    connection: sqlite3.Connection,
    directory: Path,
    project_id: str,
    registry: GenerationRegistry,
    adapter_id: str,
) -> dict[str, Any]:
    document, snapshot, readiness = current_inputs(
        connection,
        directory,
        project_id,
        registry,
        adapter_id,
    )
    versions, legacy_full = candidates(connection, project_id)
    current = {item["id"]: dependencies(snapshot, item["id"]) for item in document["paragraphs"]}
    selections = {
        row["paragraph_id"]: dict(row)
        for row in connection.execute(
            "SELECT * FROM paragraph_audio_selections WHERE project_id=?",
            (project_id,),
        )
    }
    counters: dict[str, int] = {}
    for version in versions:
        paragraph_id = version["paragraph_id"]
        counters[paragraph_id] = counters.get(paragraph_id, 0) + 1
        version["number"] = counters[paragraph_id]
        if paragraph_id in current:
            reasons, known = compare(version["dependencies"], current[paragraph_id])
        else:
            reasons, known = ["该段已从当前文案删除"], True
        version.update(
            stale_reasons=reasons,
            comparison_complete=known,
            comparison="stale" if reasons else "matching" if known else "unknown",
        )
        try:
            require_audio(directory, version)
            version["available"] = True
        except (OSError, TaskError, ValueError, KeyError):
            version["available"] = False
    token = value_digest(
        {
            "document": document,
            "reference": snapshot["reference"],
            "adapter": snapshot["adapter"],
            "readiness": readiness,
        }
    )
    return {
        "comparison_token": token,
        "adapter_id": adapter_id,
        "document_revision": document["revision"],
        "confirmation_current": document["confirmation_current"],
        "readiness": readiness,
        "paragraphs": [
            {
                "id": item["id"],
                "draft_text": item["draft_text"],
                "selection": selections.get(item["id"]),
                "versions": [v for v in reversed(versions) if v["paragraph_id"] == item["id"]],
            }
            for item in document["paragraphs"]
        ],
        "archived_versions": [v for v in reversed(versions) if v["paragraph_id"] not in current],
        "legacy_full_count": legacy_full,
    }


class SelectVersion(StrictModel):
    version_id: UUID
    expected_revision: Revision
    adapter_id: str = Field(min_length=1, max_length=100)
    comparison_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    acknowledge_mismatch: StrictBool = False


def version_router(
    database: Path,
    directory: Path,
    require_storage: Callable[[], None],
    registry: GenerationRegistry,
) -> APIRouter:
    routes = APIRouter(prefix="/api/projects/{project_id}")

    @routes.get("/paragraph-versions")
    def history(
        project_id: UUID,
        adapter_id: str = Query(min_length=1, max_length=100),
    ) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            connection.execute("BEGIN")
            return catalogue(connection, directory, str(project_id), registry, adapter_id)

    @routes.put("/paragraphs/{paragraph_id}/selection")
    def select(project_id: UUID, paragraph_id: UUID, payload: SelectVersion) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            catalog = catalogue(
                connection, directory, str(project_id), registry, payload.adapter_id
            )
            paragraph = next(
                (p for p in catalog["paragraphs"] if p["id"] == str(paragraph_id)), None
            )
            if paragraph is None:
                raise HTTPException(404)
            selected = paragraph["selection"]
            revision = selected["revision"] if selected else 0
            if revision != payload.expected_revision:
                raise TaskError(409, "selection_conflict", "选用版本已被其他窗口修改，请刷新后核对")
            if catalog["comparison_token"] != payload.comparison_token:
                raise TaskError(409, "comparison_changed", "比较所依据的输入已改变，请刷新后核对")
            version = next(
                (v for v in paragraph["versions"] if v["id"] == str(payload.version_id)), None
            )
            if version is None:
                raise HTTPException(404)
            if not version["available"]:
                raise TaskError(409, "version_unavailable", "音频文件不可用，原选用版本保留")
            if version["comparison"] != "matching" and not payload.acknowledge_mismatch:
                raise TaskError(
                    409, "mismatch_acknowledgement_required", "请明确仍选用过时或无法核定的版本"
                )
            now = timestamp()
            connection.execute(
                "INSERT INTO paragraph_audio_selections VALUES (?,?,?,?,?) "
                "ON CONFLICT(project_id,paragraph_id) DO UPDATE SET "
                "version_id=excluded.version_id,revision=excluded.revision,updated_at=excluded.updated_at",
                (str(project_id), str(paragraph_id), str(payload.version_id), revision + 1, now),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE id=?", (now, str(project_id))
            )
            return {
                "paragraph_id": str(paragraph_id),
                "version_id": str(payload.version_id),
                "revision": revision + 1,
                "updated_at": now,
            }

    @routes.get("/paragraph-versions/{version_id}/audio")
    def audio(project_id: UUID, version_id: UUID) -> FileResponse:
        require_storage()
        with connect(database) as connection:
            require_project(connection, str(project_id))
            versions, _ = candidates(connection, str(project_id))
            version = next((v for v in versions if v["id"] == str(version_id)), None)
        if version is None:
            raise HTTPException(404)
        try:
            path = require_audio(directory, version)
        except (OSError, ValueError, KeyError, TaskError) as exc:
            raise TaskError(
                409, "version_unavailable", "段落音频不可用，请核对本地结果文件"
            ) from exc
        return FileResponse(
            path,
            media_type="audio/wav",
            filename=f"paragraph-{version_id}.wav",
            content_disposition_type="inline",
        )

    return routes
