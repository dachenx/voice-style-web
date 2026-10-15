"""Revision-controlled editing, immutable confirmations and conversion CAS."""

import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Query
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from .references import require_project, timestamp
from .storage import connect
from .text_converters import ConverterRegistry
from .text_models import (
    ConfirmText,
    ConversionParagraph,
    ConversionRequest,
    ConversionResult,
    ConvertText,
    SaveText,
    TextContent,
)

logger = logging.getLogger(__name__)


class TextError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def conflict() -> TextError:
    return TextError(409, "revision_conflict", "文案已更新，请重新载入后再操作")


def load_document(connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    require_project(connection, project_id)
    row = connection.execute(
        "SELECT revision, content_json FROM text_documents WHERE project_id = ?", (project_id,)
    ).fetchone()
    content = (
        json.loads(row["content_json"])
        if row
        else {
            **TextContent().model_dump(mode="json", by_alias=True),
            "conversion_warnings": [],
        }
    )
    revision = row["revision"] if row else 0
    confirmed = connection.execute(
        "SELECT snapshot_json FROM text_confirmations WHERE project_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    latest = json.loads(confirmed["snapshot_json"]) if confirmed else None
    return {
        "revision": revision,
        **content,
        "latest_confirmation": latest,
        "confirmation_current": latest is not None and latest["revision"] == revision,
    }


def write_content(
    connection: sqlite3.Connection,
    project_id: str,
    expected_revision: int,
    content: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    current = load_document(connection, project_id)
    if current["revision"] != expected_revision:
        raise conflict()
    connection.execute(
        "INSERT INTO text_documents VALUES (?, ?, ?) ON CONFLICT(project_id) "
        "DO UPDATE SET revision=excluded.revision, content_json=excluded.content_json",
        (
            project_id,
            expected_revision + 1,
            json.dumps({**content, "conversion_warnings": warnings}, ensure_ascii=True),
        ),
    )
    connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (timestamp(), project_id))
    return load_document(connection, project_id)


def text_router(
    database: Path, require_storage: Callable[[], None], registry: ConverterRegistry
) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/text-converters")
    def converters() -> dict[str, Any]:
        capabilities = registry.capabilities()
        return {
            "converters": capabilities,
            "message": "已注册转换组件；转换结果仍需人工核对"
            if capabilities
            else "尚未配置自动转换组件，请人工填写武汉话草稿；当前不提供真实自动转换",
        }

    @routes.get("/api/projects/{project_id}/text")
    def get_text(project_id: UUID) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            connection.execute("BEGIN")
            return load_document(connection, str(project_id))

    @routes.put("/api/projects/{project_id}/text")
    def save_text(project_id: UUID, payload: SaveText) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            return write_content(
                connection,
                str(project_id),
                payload.expected_revision,
                payload.model_dump(mode="json", by_alias=True, exclude={"expected_revision"}),
                [],
            )

    @routes.post("/api/projects/{project_id}/text/confirm")
    def confirm(project_id: UUID, payload: ConfirmText) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            document = load_document(connection, str(project_id))
            if document["revision"] != payload.expected_revision:
                raise conflict()
            if (
                document["revision"] == 0
                or not document["paragraphs"]
                or any(
                    not paragraph["original_text"].strip() or not paragraph["draft_text"].strip()
                    for paragraph in document["paragraphs"]
                )
            ):
                raise TextError(
                    422, "incomplete_text", "请先保存完整文案，每段原稿和草稿均不能为空"
                )
            if document["confirmation_current"]:
                return document
            snapshot = {
                key: value
                for key, value in document.items()
                if key not in {"latest_confirmation", "confirmation_current"}
            }
            snapshot.update(id=str(uuid4()), created_at=timestamp())
            connection.execute(
                "INSERT INTO text_confirmations VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot["id"],
                    str(project_id),
                    document["revision"],
                    snapshot["created_at"],
                    json.dumps(snapshot, ensure_ascii=True),
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (snapshot["created_at"], str(project_id)),
            )
            return load_document(connection, str(project_id))

    @routes.get("/api/projects/{project_id}/text/confirmations")
    def confirmations(project_id: UUID) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            require_project(connection, str(project_id))
            rows = connection.execute(
                "SELECT id, revision, created_at FROM text_confirmations WHERE project_id = ? "
                "ORDER BY revision DESC",
                (str(project_id),),
            )
            return {"confirmations": [dict(row) for row in rows]}

    @routes.get("/api/projects/{project_id}/text/confirmations/{confirmation_id}")
    def confirmation_detail(project_id: UUID, confirmation_id: UUID) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            require_project(connection, str(project_id))
            row = connection.execute(
                "SELECT snapshot_json FROM text_confirmations WHERE id = ? AND project_id = ?",
                (str(confirmation_id), str(project_id)),
            ).fetchone()
        if row is None:
            raise HTTPException(404)
        snapshot: dict[str, Any] = json.loads(row["snapshot_json"])
        return snapshot

    @routes.get("/api/projects/{project_id}/text/confirmed-input")
    def confirmed_input(
        project_id: UUID,
        expected_revision: Annotated[
            str,
            Query(
                pattern=r"^(0|[1-9][0-9]*)$",
                max_length=20,
            ),
        ],
    ) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            connection.execute("BEGIN")
            document = load_document(connection, str(project_id))
            if document["revision"] != int(expected_revision):
                raise conflict()
            if not document["confirmation_current"]:
                raise TextError(
                    409, "confirmation_required", "当前文案尚未人工确认或确认已过时，请重新核对确认"
                )
            snapshot: dict[str, Any] = document["latest_confirmation"]
            return snapshot

    @routes.post("/api/projects/{project_id}/text/convert")
    def convert(project_id: UUID, payload: ConvertText) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            connection.execute("BEGIN")
            document = load_document(connection, str(project_id))
        if document["revision"] != payload.expected_revision:
            raise conflict()
        converter = registry.converters.get(payload.converter_id)
        if converter is None:
            raise TextError(
                503, "converter_unavailable", "自动转换组件不可用，请人工填写草稿或配置组件"
            )
        if (
            document["source_language"] not in converter.capability.source_languages
            or document["target_language"] not in converter.capability.target_languages
        ):
            raise TextError(422, "converter_unsupported", "该组件不支持当前语言转换")
        if not document["paragraphs"] or any(
            not paragraph["original_text"].strip() for paragraph in document["paragraphs"]
        ):
            raise TextError(422, "incomplete_text", "请先保存非空原稿，再进行转换")
        request = ConversionRequest(
            source_language=document["source_language"],
            target_language=document["target_language"],
            register=document["register"],
            terms=document["terms"],
            paragraphs=tuple(
                ConversionParagraph(id=paragraph["id"], original_text=paragraph["original_text"])
                for paragraph in document["paragraphs"]
            ),
        )
        try:
            raw_result = converter.convert(request)
        except Exception as exc:
            logger.exception("文本转换组件调用失败")
            raise TextError(
                502, "converter_failed", "自动转换失败，原稿和已有草稿已保留，请稍后重试"
            ) from exc
        try:
            result = ConversionResult.model_validate(
                raw_result.model_dump() if isinstance(raw_result, ConversionResult) else raw_result
            )
            ids = [paragraph.id for paragraph in result.paragraphs]
            if len(set(ids)) != len(ids) or set(ids) != {
                paragraph.id for paragraph in request.paragraphs
            }:
                raise ValueError("转换段落 ID 不匹配")
            drafts = {str(paragraph.id): paragraph.draft_text for paragraph in result.paragraphs}
            content = TextContent.model_validate(
                {
                    "source_language": document["source_language"],
                    "target_language": document["target_language"],
                    "register": document["register"],
                    "terms": document["terms"],
                    "paragraphs": [
                        {**paragraph, "draft_text": drafts[paragraph["id"]]}
                        for paragraph in document["paragraphs"]
                    ],
                }
            )
        except (ValidationError, ValueError, TypeError, AttributeError) as exc:
            logger.exception("文本转换组件返回无效结果")
            raise TextError(
                502, "converter_invalid_output", "转换组件返回无效结果，已有文案已保留"
            ) from exc
        with connect(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            return write_content(
                connection,
                str(project_id),
                payload.expected_revision,
                content.model_dump(mode="json", by_alias=True),
                list(result.warnings),
            )

    return routes
