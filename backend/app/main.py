"""Project and reference audio HTTP API with optional built frontend."""

import logging
import os
import sqlite3
import unicodedata
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, field_validator
from starlette.exceptions import HTTPException

from .audio import AudioError
from .directory_lock import DirectoryLock
from .exports import cleanup_exports, export_router
from .generation import GenerationAdapter, GenerationRegistry, TaskError
from .references import cleanup_uncommitted, router
from .storage import connect, migrate
from .tasks import task_router
from .text_converters import ConverterRegistry, TextConverter
from .text_documents import TextError, text_router
from .transcripts import transcript_router
from .versions import version_router
from .worker_manager import WorkerManager

REPO_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise ValueError("项目名称不能包含控制字符")
        value = value.strip()
        if not 1 <= len(value) <= 80:
            raise ValueError("项目名称去除首尾空白后须为 1–80 个字符")
        return value


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_app(
    data_dir: str | Path | None = None,
    *,
    text_converters: Sequence[TextConverter] = (),
    worker_enabled: bool = True,
    generation_adapters: Sequence[GenerationAdapter] | None = None,
    model_config_path: Path | None = None,
) -> FastAPI:
    directory = Path(data_dir or os.environ.get("VOICE_DATA_DIR") or REPO_ROOT / "data")
    database = directory / "projects.sqlite3"
    if generation_adapters is not None and worker_enabled:
        raise ValueError("注入测试生成适配器时须关闭自动工作进程")
    generation_registry = GenerationRegistry(
        generation_adapters,
        model_config_path=model_config_path,
        runtime_directory=directory / "model-health",
    )
    worker = WorkerManager(directory, worker_enabled, model_config_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.storage_ready = False
        directory_lock = DirectoryLock(directory)
        try:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                directory_lock.acquire()
                migrate(database)
                cleanup_uncommitted(directory, database)
                cleanup_exports(directory)
                application.state.storage_ready = True
                worker.start()
            except (OSError, sqlite3.Error):
                # Serve an actionable, path-free error rather than exposing startup details.
                logger.exception("本地存储初始化失败")
            yield
        finally:
            worker.close()
            directory_lock.close()

    application = FastAPI(title="Voice Style Web", lifespan=lifespan)
    registry = ConverterRegistry(text_converters)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error(
            422,
            "invalid_input",
            "输入无效：请检查字段类型、名称、UUID、文字长度及整数版本或起止时间",
        )

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return error(404, "not_found", "项目或接口不存在")
        return error(exc.status_code, "http_error", "请求无法处理")

    @application.exception_handler(sqlite3.Error)
    @application.exception_handler(OSError)
    async def storage_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("本地存储请求失败", exc_info=(type(exc), exc, exc.__traceback__))
        return error(
            503,
            "storage_unavailable",
            "本地存储暂不可用，请检查目录权限或是否已有服务使用该目录，再重启服务",
        )

    @application.exception_handler(AudioError)
    async def audio_error(request: Request, exc: AudioError) -> JSONResponse:
        return error(exc.status, exc.code, exc.message)

    @application.exception_handler(TextError)
    async def text_error(request: Request, exc: TextError) -> JSONResponse:
        return error(exc.status, exc.code, exc.message)

    @application.exception_handler(TaskError)
    async def task_error(request: Request, exc: TaskError) -> JSONResponse:
        return error(exc.status, exc.code, exc.message)

    def require_storage() -> None:
        if not application.state.storage_ready:
            raise sqlite3.OperationalError("storage unavailable")

    @application.get("/api/health")
    def health() -> dict[str, str]:
        require_storage()
        with connect(database) as connection:
            connection.execute("SELECT 1 FROM projects LIMIT 1")
        return {"status": "ok"}

    @application.get("/api/projects")
    def list_projects() -> dict[str, list[dict[str, Any]]]:
        require_storage()
        with connect(database) as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY updated_at DESC, id ASC")
            return {"projects": [dict(row) for row in rows]}

    @application.post("/api/projects", status_code=201)
    def create_project(payload: ProjectInput) -> dict[str, str]:
        require_storage()
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        project = dict(
            id=str(uuid4()), name=payload.name, created_at=timestamp, updated_at=timestamp
        )
        with connect(database) as connection:
            connection.execute(
                "INSERT INTO projects VALUES (:id, :name, :created_at, :updated_at)", project
            )
        return project

    @application.get("/api/projects/{project_id}")
    def get_project(project_id: UUID) -> dict[str, Any]:
        require_storage()
        with connect(database) as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (str(project_id),)
            ).fetchone()
        if row is None:
            raise HTTPException(404)
        return dict(row)

    @application.patch("/api/projects/{project_id}")
    def rename_project(project_id: UUID, payload: ProjectInput) -> dict[str, Any]:
        require_storage()
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        with connect(database) as connection:
            cursor = connection.execute(
                "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
                (payload.name, timestamp, str(project_id)),
            )
            if cursor.rowcount == 0:
                raise HTTPException(404)
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (str(project_id),)
            ).fetchone()
        assert row is not None
        return dict(row)

    application.include_router(
        export_router(database, directory, require_storage, generation_registry)
    )
    application.include_router(router(directory, database, require_storage))
    application.include_router(text_router(database, require_storage, registry))
    application.include_router(transcript_router(database, directory, require_storage))
    application.include_router(
        version_router(database, directory, require_storage, generation_registry)
    )
    application.include_router(
        task_router(database, directory, require_storage, generation_registry, worker.health)
    )

    @application.api_route(
        "/api", methods=["GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"]
    )
    @application.api_route(
        "/api/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE", "HEAD", "OPTIONS"]
    )
    def missing_api(path: str = "") -> JSONResponse:
        return error(404, "not_found", "接口不存在")

    frontend = REPO_ROOT / "frontend" / "dist"
    if frontend.is_dir():
        application.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return application


app = create_app()
