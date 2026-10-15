"""Reference uploads, immutable clips and current selection."""

import asyncio
import hashlib
import shutil
import sqlite3
import unicodedata
import wave
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, StrictInt
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from .audio import FORMATS, MAX_BYTES, SAMPLE_RATE, AudioError, decode, excerpt
from .storage import connect


class ClipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_ms: StrictInt
    end_ms: StrictInt


class SelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clip_id: UUID


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def available(path: Path) -> Path:
    if not path.is_file():
        raise OSError("音频文件不可用")
    return path


def require_project(connection: sqlite3.Connection, project_id: str) -> None:
    if connection.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
        raise HTTPException(404)


def require_reference(
    connection: sqlite3.Connection, project_id: str, reference_id: str
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM audio_references WHERE id = ? AND project_id = ?",
        (reference_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404)
    return dict(row)


def cleanup_uncommitted(directory: Path, database: Path) -> None:
    """Recover file operations interrupted before their metadata transaction committed."""
    temporary = directory / "audio-tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    root = directory / "references"
    if not root.exists():
        return
    with connect(database) as connection:
        references = {row[0] for row in connection.execute("SELECT id FROM audio_references")}
        clips = {row[0] for row in connection.execute("SELECT id FROM reference_clips")}
    for reference in root.iterdir():
        if reference.name not in references:
            if reference.is_dir():
                shutil.rmtree(reference)
            else:
                reference.unlink()
        elif (reference / "clips").is_dir():
            for clip in (reference / "clips").iterdir():
                if clip.stem not in clips:
                    clip.unlink()


def router(directory: Path, database: Path, require_storage: Callable[[], None]) -> APIRouter:
    routes = APIRouter(prefix="/api/projects/{project_id}")
    operation = asyncio.Lock()

    @routes.get("/references")
    def list_references(project_id: UUID) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            require_project(connection, str(project_id))
            references = []
            for row in connection.execute(
                "SELECT * FROM audio_references WHERE project_id = ? "
                "ORDER BY created_at DESC, id ASC",
                (str(project_id),),
            ):
                reference = dict(row)
                reference["clips"] = [
                    dict(clip)
                    for clip in connection.execute(
                        "SELECT * FROM reference_clips WHERE reference_id = ? "
                        "ORDER BY created_at DESC, id ASC",
                        (row["id"],),
                    )
                ]
                references.append(reference)
            selection = connection.execute(
                "SELECT clip_id FROM project_reference_selection WHERE project_id = ?",
                (str(project_id),),
            ).fetchone()
        return {"references": references, "active_clip_id": selection[0] if selection else None}

    def commit_reference(
        project_id: str, reference_id: str, filename: str, stage: Path, byte_size: int, digest: str
    ) -> dict[str, Any]:
        duration = decode(stage / "original", stage / "preview.wav", Path(filename).suffix.lower())
        reference = dict(
            id=reference_id,
            project_id=project_id,
            filename=filename,
            byte_size=byte_size,
            sha256=digest,
            duration_ms=duration,
            created_at=timestamp(),
        )
        root = directory / "references"
        root.mkdir(exist_ok=True)
        target = root / reference_id
        try:
            stage.rename(target)
            with connect(database) as connection:
                require_project(connection, project_id)
                connection.execute(
                    "INSERT INTO audio_references VALUES "
                    "(:id, :project_id, :filename, :byte_size, :sha256, :duration_ms, :created_at)",
                    reference,
                )
                connection.execute(
                    "UPDATE projects SET updated_at = ? WHERE id = ?",
                    (reference["created_at"], project_id),
                )
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            raise
        return {**reference, "clips": []}

    @routes.post("/references", status_code=201)
    async def upload(project_id: UUID, request: Request, filename: str) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            require_project(connection, str(project_id))
        if (
            not 1 <= len(filename) <= 255
            or "/" in filename
            or "\\" in filename
            or any(unicodedata.category(char) == "Cc" for char in filename)
            or Path(filename).suffix.lower() not in FORMATS
        ):
            raise AudioError("文件名须为 WAV、MP3、M4A、FLAC 或 OGG，且不能包含路径或控制字符")
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
            raise AudioError("请以原始音频文件上传", 415, "invalid_media_type")
        if operation.locked():
            raise AudioError("正在处理另一份音频，请稍后重试", 429, "audio_busy")
        async with operation:
            reference_id = str(uuid4())
            stage = directory / "audio-tmp" / reference_id
            digest = hashlib.sha256()
            size = 0
            try:
                await run_in_threadpool(stage.mkdir, parents=True)
                with (stage / "original").open("wb") as output:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise AudioError("音频文件不能超过 50 MiB", 413, "audio_too_large")
                        digest.update(chunk)
                        await run_in_threadpool(output.write, chunk)
                if size == 0:
                    raise AudioError("音频文件为空")
                return await run_in_threadpool(
                    commit_reference,
                    str(project_id),
                    reference_id,
                    filename,
                    stage,
                    size,
                    digest.hexdigest(),
                )
            finally:
                if stage.exists():
                    await run_in_threadpool(shutil.rmtree, stage)

    @routes.get("/references/{reference_id}/original")
    def original(project_id: UUID, reference_id: UUID) -> FileResponse:
        require_storage()
        with connect(database) as connection:
            reference = require_reference(connection, str(project_id), str(reference_id))
        return FileResponse(
            available(directory / "references" / str(reference_id) / "original"),
            media_type="application/octet-stream",
            filename=reference["filename"],
        )

    @routes.get("/references/{reference_id}/preview")
    def preview(project_id: UUID, reference_id: UUID) -> FileResponse:
        require_storage()
        with connect(database) as connection:
            require_reference(connection, str(project_id), str(reference_id))
        return FileResponse(
            available(directory / "references" / str(reference_id) / "preview.wav"),
            media_type="audio/wav",
        )

    @routes.post("/references/{reference_id}/clips", status_code=201)
    async def save_clip(project_id: UUID, reference_id: UUID, payload: ClipInput) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            reference = require_reference(connection, str(project_id), str(reference_id))
        if not (
            0 <= payload.start_ms < payload.end_ms <= reference["duration_ms"]
            and payload.end_ms - payload.start_ms >= 100
        ):
            raise AudioError(
                "选段须在音频时长内，起点小于终点，且至少 100 毫秒", code="invalid_clip"
            )
        if operation.locked():
            raise AudioError("正在处理另一份音频，请稍后重试", 429, "audio_busy")
        async with operation:

            def save() -> dict[str, Any]:
                clip = dict(
                    id=str(uuid4()),
                    reference_id=str(reference_id),
                    start_ms=payload.start_ms,
                    end_ms=payload.end_ms,
                    created_at=timestamp(),
                )
                root = directory / "references" / str(reference_id)
                clips = root / "clips"
                clips.mkdir(exist_ok=True)
                target = clips / f"{clip['id']}.wav"
                try:
                    excerpt(root / "preview.wav", target, payload.start_ms, payload.end_ms)
                    with connect(database) as connection:
                        require_reference(connection, str(project_id), str(reference_id))
                        connection.execute(
                            "INSERT INTO reference_clips VALUES "
                            "(:id, :reference_id, :start_ms, :end_ms, :created_at)",
                            clip,
                        )
                        connection.execute(
                            "INSERT INTO project_reference_selection VALUES (?, ?) "
                            "ON CONFLICT(project_id) DO UPDATE SET clip_id=excluded.clip_id",
                            (str(project_id), clip["id"]),
                        )
                        connection.execute(
                            "UPDATE projects SET updated_at = ? WHERE id = ?",
                            (clip["created_at"], str(project_id)),
                        )
                except BaseException:
                    target.unlink(missing_ok=True)
                    raise
                return clip

            return await run_in_threadpool(save)

    @routes.get("/references/{reference_id}/clips/{clip_id}/audio")
    def clip_audio(project_id: UUID, reference_id: UUID, clip_id: UUID) -> FileResponse:
        require_storage()
        with connect(database) as connection:
            require_reference(connection, str(project_id), str(reference_id))
            if (
                connection.execute(
                    "SELECT id FROM reference_clips WHERE id = ? AND reference_id = ?",
                    (str(clip_id), str(reference_id)),
                ).fetchone()
                is None
            ):
                raise HTTPException(404)
        return FileResponse(
            available(directory / "references" / str(reference_id) / "clips" / f"{clip_id}.wav"),
            media_type="audio/wav",
            filename=f"{clip_id}.wav",
            content_disposition_type="inline",
        )

    @routes.put("/active-clip")
    def activate(project_id: UUID, payload: SelectionInput) -> dict[str, str]:
        require_storage()
        with connect(database) as connection:
            require_project(connection, str(project_id))
            selected = connection.execute(
                "SELECT c.id, c.reference_id, c.start_ms, c.end_ms "
                "FROM reference_clips c JOIN audio_references r "
                "ON c.reference_id = r.id WHERE c.id = ? AND r.project_id = ?",
                (str(payload.clip_id), str(project_id)),
            ).fetchone()
            if selected is None:
                raise HTTPException(404)
            target = available(
                directory
                / "references"
                / selected["reference_id"]
                / "clips"
                / f"{payload.clip_id}.wav"
            )
            try:
                with wave.open(str(target), "rb") as audio:
                    expected = (selected["end_ms"] - selected["start_ms"]) * SAMPLE_RATE // 1000
                    if (
                        audio.getnchannels() != 1
                        or audio.getsampwidth() != 2
                        or audio.getframerate() != SAMPLE_RATE
                        or audio.getnframes() != expected
                        or len(audio.readframes(expected)) != expected * 2
                    ):
                        raise OSError("参考片段文件不完整")
            except (wave.Error, EOFError) as exc:
                raise OSError("参考片段文件无法读取") from exc
            connection.execute(
                "INSERT INTO project_reference_selection VALUES (?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET clip_id=excluded.clip_id",
                (str(project_id), str(payload.clip_id)),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (timestamp(), str(project_id))
            )
        return {"active_clip_id": str(payload.clip_id)}

    return routes
