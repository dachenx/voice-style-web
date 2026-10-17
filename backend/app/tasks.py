"""Persistent task submission with transactional snapshots and idempotency."""

import hashlib
import json
import sqlite3
import wave
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException

from .audio import SAMPLE_RATE
from .generation import (
    PARAGRAPH_OUTPUT_CONTRACT,
    GenerationRegistry,
    RetryInput,
    TaskError,
    TaskInput,
)
from .local_model import LocalCosyVoiceAdapter
from .references import available, require_project, require_reference, timestamp
from .storage import connect
from .text_documents import conflict, load_document


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_clip(path: Path, start_ms: int, end_ms: int) -> str:
    available(path)
    try:
        with wave.open(str(path), "rb") as audio:
            expected = (end_ms - start_ms) * SAMPLE_RATE // 1000
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getframerate() != SAMPLE_RATE
                or audio.getnframes() != expected
                or len(audio.readframes(expected)) != expected * 2
            ):
                raise OSError("参考片段不完整")
    except (wave.Error, EOFError) as exc:
        raise OSError("参考片段无法读取") from exc
    return file_digest(path)


def task_from_row(row: sqlite3.Row, *, detailed: bool = True) -> dict[str, Any]:
    task = dict(row)
    snapshot = json.loads(task.pop("snapshot_json"))
    task["mode"] = snapshot["mode"]
    task["paragraph_id"] = snapshot["paragraph_ids"][0] if snapshot["mode"] == "preview" else None
    task["adapter"] = snapshot["adapter"]
    task["is_simulated"] = snapshot.get("is_simulated", True)
    task["snapshot_sha256"] = hashlib.sha256(canonical(snapshot).encode("utf-8")).hexdigest()
    code, message = task.pop("error_code"), task.pop("error_message")
    task["error"] = {"code": code, "message": message} if code else None
    raw_result = task.pop("result_json")
    task["result"] = json.loads(raw_result) if raw_result else None
    if detailed:
        task["snapshot"] = snapshot
    return task


def get_task(connection: sqlite3.Connection, project_id: str, task_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM generation_tasks WHERE id = ? AND project_id = ?", (task_id, project_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404)
    return task_from_row(row)


def request_task(
    connection: sqlite3.Connection, project_id: str, request_id: str, fingerprint: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT fingerprint, task_id FROM generation_requests "
        "WHERE project_id = ? AND request_id = ?",
        (project_id, request_id),
    ).fetchone()
    if row is None:
        return None
    if row["fingerprint"] != fingerprint:
        raise TaskError(409, "request_id_conflict", "请求 ID 已用于其他内容，请重新发起操作")
    return get_task(connection, project_id, row["task_id"])


def remember_request(
    connection: sqlite3.Connection, project_id: str, request_id: str, fingerprint: str, task_id: str
) -> None:
    connection.execute(
        "INSERT INTO generation_requests VALUES (?, ?, ?, ?)",
        (project_id, request_id, fingerprint, task_id),
    )


def insert_task(
    connection: sqlite3.Connection,
    project_id: str,
    snapshot: dict[str, Any],
    retry_of: str | None = None,
) -> dict[str, Any]:
    task_id, now = str(uuid4()), timestamp()
    connection.execute(
        "INSERT INTO generation_tasks (id,project_id,state,phase,total_paragraphs,created_at,"
        "updated_at,snapshot_json,retry_of) VALUES (?,?,'queued','queued',?,?,?,?,?)",
        (
            task_id,
            project_id,
            len(snapshot["paragraph_ids"]),
            now,
            now,
            canonical(snapshot),
            retry_of,
        ),
    )
    connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
    return get_task(connection, project_id, task_id)


def enqueue(
    database: Path,
    directory: Path,
    project_id: str,
    payload: TaskInput,
    registry: GenerationRegistry,
) -> tuple[dict[str, Any], bool]:
    request_content = payload.model_dump(mode="json", exclude={"request_id"})
    # Preserve canonical M04 request fingerprints, including full-mode paragraph_id=null.
    for key in (
        "expected_transcript_revision",
        "expected_adapter_version",
        "expected_config_fingerprint",
    ):
        if request_content[key] is None:
            request_content.pop(key)
    fingerprint = canonical({"operation": "submit", **request_content})
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        require_project(connection, project_id)
        existing = request_task(connection, project_id, str(payload.request_id), fingerprint)
        if existing is not None:
            return existing, False
        document = load_document(connection, project_id)
        if document["revision"] != payload.expected_revision:
            raise conflict()
        confirmation = document["latest_confirmation"]
        if not document["confirmation_current"] or not confirmation:
            raise TaskError(409, "confirmation_required", "当前文案尚未人工确认或确认已过时")
        if confirmation["id"] != str(payload.expected_confirmation_id):
            raise TaskError(409, "confirmation_conflict", "确认版本已改变，请重新核对")
        selected = connection.execute(
            "SELECT clip_id FROM project_reference_selection WHERE project_id = ?", (project_id,)
        ).fetchone()
        if selected is None or selected["clip_id"] != str(payload.expected_clip_id):
            raise TaskError(
                409, "reference_conflict", "当前参考片段已改变或尚未选择，请刷新素材核对"
            )
        clip_row = connection.execute(
            "SELECT * FROM reference_clips WHERE id = ?", (str(payload.expected_clip_id),)
        ).fetchone()
        if clip_row is None:
            raise TaskError(409, "reference_conflict", "当前参考片段不可用，请重新选择")
        clip = dict(clip_row)
        reference = require_reference(connection, project_id, clip["reference_id"])
        path = directory / "references" / reference["id"] / "clips" / f"{clip['id']}.wav"
        digest = validate_clip(path, clip["start_ms"], clip["end_ms"])
        adapter = registry.require(payload.adapter_id)
        transcript = None
        if not adapter.capability.is_simulated:
            from .transcripts import transcript_value

            if not isinstance(adapter, LocalCosyVoiceAdapter):
                raise TaskError(503, "adapter_unavailable", "真实适配器未接入本机配置协议")
            configuration = adapter.require_configuration()
            if (
                payload.expected_adapter_version != adapter.capability.version
                or payload.expected_config_fingerprint != configuration["config_fingerprint"]
            ):
                raise TaskError(
                    409, "model_configuration_conflict", "模型版本或配置已改变，请刷新组件信息核对"
                )
            if clip["end_ms"] - clip["start_ms"] > adapter.capability.reference_max_duration_ms:
                raise TaskError(422, "invalid_reference_duration", "本适配器参考片段不能超过30秒")
            transcript = transcript_value(connection, clip["id"], digest)
            if (
                payload.expected_transcript_revision is None
                or payload.expected_transcript_revision != transcript["revision"]
            ):
                raise TaskError(409, "transcript_conflict", "参考原话版本不一致，请保存并重新核对")
            if transcript["revision"] == 0 or not transcript["text"].strip():
                raise TaskError(
                    422, "transcript_required", "请填写并保存当前参考片段实际说出的准确原话"
                )
        if payload.mode not in adapter.capability.modes:
            raise TaskError(422, "unsupported_mode", "该模拟适配器不支持此模式")
        ids = [paragraph["id"] for paragraph in confirmation["paragraphs"]]
        if payload.mode == "preview":
            if str(payload.paragraph_id) not in ids:
                raise TaskError(422, "invalid_paragraph", "短段不属于当前确认文案")
            ids = [str(payload.paragraph_id)]
        snapshot = {
            "confirmation": confirmation,
            "reference": {**reference, "clip": clip, "clip_sha256": digest},
            "adapter": adapter.capability.model_dump(mode="json"),
            "parameters": {},
            "mode": payload.mode,
            "paragraph_ids": ids,
            "is_simulated": adapter.capability.is_simulated,
            "output_contract": PARAGRAPH_OUTPUT_CONTRACT,
        }
        if transcript is not None:
            snapshot["reference"]["transcript"] = transcript
        task = insert_task(connection, project_id, snapshot)
        remember_request(connection, project_id, str(payload.request_id), fingerprint, task["id"])
        return task, True


def retry_task(
    database: Path, project_id: str, task_id: str, payload: RetryInput
) -> tuple[dict[str, Any], bool]:
    fingerprint = canonical({"operation": "retry", "task_id": task_id})
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        require_project(connection, project_id)
        existing = request_task(connection, project_id, str(payload.request_id), fingerprint)
        if existing is not None:
            return existing, False
        parent = get_task(connection, project_id, task_id)
        if parent["state"] != "failed":
            raise TaskError(409, "task_not_retryable", "只有失败任务可以重试")
        child = connection.execute(
            "SELECT * FROM generation_tasks WHERE retry_of = ?", (task_id,)
        ).fetchone()
        task = (
            task_from_row(child)
            if child is not None
            else insert_task(connection, project_id, parent["snapshot"], task_id)
        )
        remember_request(connection, project_id, str(payload.request_id), fingerprint, task["id"])
        return task, child is None


def worker_health(database: Path) -> dict[str, Any]:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT state,last_seen_at FROM generation_worker_health WHERE singleton=1"
        ).fetchone()
    if row is None:
        return {"state": "stopped", "last_seen_at": None, "error": None}
    age = (
        datetime.now(UTC) - datetime.fromisoformat(row["last_seen_at"].replace("Z", "+00:00"))
    ).total_seconds()
    state = row["state"] if age < 5 else "unavailable"
    error = (
        {"code": "worker_unavailable", "message": "工作进程暂无响应，任务已保留，请重启服务"}
        if state == "unavailable"
        else None
    )
    return {"state": state, "last_seen_at": row["last_seen_at"], "error": error}


def task_router(
    database: Path,
    directory: Path,
    require_storage: Callable[[], None],
    registry: GenerationRegistry,
    health: Callable[[], dict[str, Any]] | None = None,
) -> APIRouter:
    routes = APIRouter()

    def read_health() -> dict[str, Any]:
        return health() if health else worker_health(database)

    @routes.get("/api/generation-adapters")
    def adapters() -> dict[str, Any]:
        return {
            "adapters": registry.capabilities(),
            "message": "模拟组件用于流程验证；真实组件需本机配置及准确参考原话。"
            "配置检查不代表模型已加载或声音达标。",
        }

    @routes.get("/api/generation-adapters/{adapter_id}/health")
    def adapter_health(adapter_id: str) -> dict[str, Any]:
        return registry.health(adapter_id)

    @routes.get("/api/worker/health")
    def health_route() -> dict[str, Any]:
        require_storage()
        return read_health()

    @routes.get("/api/projects/{project_id}/tasks")
    def list_tasks(project_id: UUID) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            require_project(connection, str(project_id))
            rows = connection.execute(
                "SELECT * FROM generation_tasks WHERE project_id = ? "
                "ORDER BY created_at DESC,id ASC",
                (str(project_id),),
            )
            tasks = [task_from_row(row, detailed=False) for row in rows]
        return {"tasks": tasks, "worker": read_health()}

    @routes.post("/api/projects/{project_id}/tasks")
    def submit(project_id: UUID, payload: TaskInput, response: Response) -> dict[str, Any]:
        require_storage()
        task, created = enqueue(database, directory, str(project_id), payload, registry)
        response.status_code = 201 if created else 200
        return task

    @routes.get("/api/projects/{project_id}/tasks/{task_id}")
    def detail(project_id: UUID, task_id: UUID) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            return get_task(connection, str(project_id), str(task_id))

    @routes.post("/api/projects/{project_id}/tasks/{task_id}/cancel")
    def cancel(project_id: UUID, task_id: UUID) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = get_task(connection, str(project_id), str(task_id))
            if task["state"] == "canceled":
                return task
            if task["state"] != "queued":
                raise TaskError(409, "task_not_cancelable", "只有排队任务可以取消")
            now = timestamp()
            connection.execute(
                "UPDATE generation_tasks SET state='canceled',phase='canceled',"
                "updated_at=?,finished_at=?,state_version=state_version+1 "
                "WHERE id=? AND state='queued'",
                (now, now, str(task_id)),
            )
            return get_task(connection, str(project_id), str(task_id))

    @routes.post("/api/projects/{project_id}/tasks/{task_id}/retry")
    def retry(
        project_id: UUID, task_id: UUID, payload: RetryInput, response: Response
    ) -> dict[str, Any]:
        require_storage()
        task, created = retry_task(database, str(project_id), str(task_id), payload)
        response.status_code = 201 if created else 200
        return task

    def output(project_id: UUID, task_id: UUID, attachment: bool) -> FileResponse:
        require_storage()
        with connect(database) as connection:
            task = get_task(connection, str(project_id), str(task_id))
        if task["state"] != "succeeded" or task["result"] is None:
            raise TaskError(409, "result_unavailable", "任务尚无可用结果")
        path = available(directory / "generation-results" / str(task_id) / "audio.wav")
        if (
            path.stat().st_size != task["result"]["byte_size"]
            or file_digest(path) != task["result"]["sha256"]
        ):
            raise OSError("模拟结果文件缺失或已改变")
        return FileResponse(
            path,
            media_type="audio/wav",
            filename=task["result"]["filename"],
            content_disposition_type="attachment" if attachment else "inline",
        )

    @routes.get("/api/projects/{project_id}/tasks/{task_id}/audio")
    def audio(project_id: UUID, task_id: UUID) -> FileResponse:
        return output(project_id, task_id, False)

    @routes.get("/api/projects/{project_id}/tasks/{task_id}/download")
    def download(project_id: UUID, task_id: UUID) -> FileResponse:
        return output(project_id, task_id, True)

    return routes
