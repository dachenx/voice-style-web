"""Exact reference words are versioned against an immutable clip and its bytes."""

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field, field_validator
from starlette.exceptions import HTTPException

from .generation import TaskError
from .references import require_reference, timestamp
from .storage import connect
from .tasks import validate_clip
from .text_models import Revision, StrictModel, safe_text


class TranscriptInput(StrictModel):
    expected_revision: Revision
    expected_clip_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(max_length=2000)

    _safe = field_validator("text")(safe_text)


def transcript_value(connection: sqlite3.Connection, clip_id: str, digest: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM reference_transcripts WHERE clip_id=? ORDER BY revision DESC LIMIT 1",
        (clip_id,),
    ).fetchone()
    if row is None:
        return {
            "clip_id": clip_id,
            "clip_sha256": digest,
            "revision": 0,
            "text": "",
            "created_at": None,
            "updated_at": None,
        }
    if row["clip_sha256"] != digest:
        raise TaskError(409, "reference_changed", "参考文件已改变，原话不能应用到修改后的音频")
    return dict(row)


def transcript_router(
    database: Path, directory: Path, require_storage: Callable[[], None]
) -> APIRouter:
    routes = APIRouter(
        prefix="/api/projects/{project_id}/references/{reference_id}/clips/{clip_id}"
    )

    def owned(connection: sqlite3.Connection, project: UUID, reference: UUID, clip: UUID) -> str:
        require_reference(connection, str(project), str(reference))
        row = connection.execute(
            "SELECT * FROM reference_clips WHERE id=? AND reference_id=?",
            (str(clip), str(reference)),
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        path = directory / "references" / str(reference) / "clips" / f"{clip}.wav"
        return validate_clip(path, row["start_ms"], row["end_ms"])

    @routes.get("/transcript")
    def read(project_id: UUID, reference_id: UUID, clip_id: UUID) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            connection.execute("BEGIN")
            digest = owned(connection, project_id, reference_id, clip_id)
            return transcript_value(connection, str(clip_id), digest)

    @routes.put("/transcript")
    def save(
        project_id: UUID, reference_id: UUID, clip_id: UUID, payload: TranscriptInput
    ) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            digest = owned(connection, project_id, reference_id, clip_id)
            if digest != payload.expected_clip_sha256:
                raise TaskError(409, "reference_changed", "参考片段内容已改变，请重新读取并核对")
            previous = transcript_value(connection, str(clip_id), digest)
            if previous["revision"] != payload.expected_revision:
                raise TaskError(409, "transcript_conflict", "参考原话已更新，请重新载入后核对")
            now = timestamp()
            connection.execute(
                "INSERT INTO reference_transcripts VALUES (?,?,?,?,?,?)",
                (
                    str(clip_id),
                    previous["revision"] + 1,
                    digest,
                    payload.text,
                    previous["created_at"] or now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE id=?", (now, str(project_id))
            )
            return transcript_value(connection, str(clip_id), digest)

    return routes
