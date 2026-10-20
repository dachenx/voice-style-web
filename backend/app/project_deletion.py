"""Permanent project removal with a durable, retryable file cleanup journal."""

import json
import shutil
import sqlite3
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .directory_lock import DirectoryLock
from .storage import connect
from .text_documents import TextError


class DeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_name: str
    acknowledged: StrictBool


def deleted(database: Path, project: str) -> bool:
    with connect(database) as connection:
        return (
            connection.execute(
                "SELECT 1 FROM project_deletions WHERE project_id=?", (project,)
            ).fetchone()
            is not None
        )


class ProjectOperations:
    """Event-loop counters cover streaming and cleanup; never block a thread on a lock."""

    def __init__(self, app: ASGIApp, database: Path) -> None:
        self.app = app
        self.database = database
        self.active: dict[str, int] = {}
        self.removing: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        parts = scope.get("path", "").strip("/").split("/")
        if scope["type"] != "http" or len(parts) < 3 or parts[:2] != ["api", "projects"]:
            await self.app(scope, receive, send)
            return
        try:
            project = str(UUID(parts[2]))
        except ValueError:
            await self.app(scope, receive, send)
            return
        project_delete = scope["method"] == "DELETE" and len(parts) == 3
        removing = project_delete or (
            scope["method"] == "DELETE" and len(parts) == 5 and parts[3] == "tasks"
        )
        if project in self.removing or (removing and self.active.get(project, 0)):
            response = JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "project_busy",
                        "message": "项目有进行中的操作，请等上传、下载或保存完成后重试",
                    }
                },
            )
            await response(scope, receive, send)
            return
        if removing:
            self.removing.add(project)
        else:
            self.active[project] = self.active.get(project, 0) + 1
        try:
            try:
                is_deleted = await run_in_threadpool(deleted, self.database, project)
            except sqlite3.Error:
                response = JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": "storage_unavailable",
                            "message": "本地存储暂不可用，请检查服务状态",
                        }
                    },
                )
                await response(scope, receive, send)
                return
            if not project_delete and is_deleted and parts[3:] != ["deletion-status"]:
                response = JSONResponse(
                    status_code=410,
                    content={
                        "error": {
                            "code": "project_deleted",
                            "message": "该项目已永久删除，不能恢复，请返回项目列表",
                        }
                    },
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
        finally:
            if removing:
                self.removing.discard(project)
            else:
                self.active[project] -= 1
                if not self.active[project]:
                    del self.active[project]


def cleanup_project(database: Path, directory: Path, project: str) -> None:
    with connect(database) as connection:
        row = connection.execute(
            "SELECT paths_json,completed FROM project_deletions WHERE project_id=?", (project,)
        ).fetchone()
    if row is None or row["completed"]:
        return
    paths = json.loads(row["paths_json"])
    # Paths are generated from database UUIDs, never from snapshot filenames or user paths.
    for group, identifier in paths:
        if group not in {"references", "generation-results", "generation-tmp", "exports"}:
            raise OSError("invalid cleanup journal")
        if str(UUID(identifier)) != identifier or (group == "exports" and identifier != project):
            raise OSError("invalid cleanup identity")
        parent = directory / group
        if parent.is_symlink():
            raise OSError("unsafe cleanup root")
        path = parent / identifier
        if path.is_symlink():
            path.unlink()  # Remove the link, never its target.
        elif path.exists():
            shutil.rmtree(path)
    with connect(database) as connection:
        connection.execute(
            "UPDATE project_deletions SET completed=1,paths_json='[]' WHERE project_id=?",
            (project,),
        )


def resume_deletions(database: Path, directory: Path) -> None:
    lock = DirectoryLock(directory, ".project-cleanup.lock")
    try:
        lock.acquire()
    except BlockingIOError:
        return
    try:
        _resume_deletions(database, directory)
        from .task_deletion import resume_tasks_locked

        resume_tasks_locked(database, directory)
    finally:
        lock.close()


def _resume_deletions(database: Path, directory: Path) -> None:
    with connect(database) as connection:
        projects = connection.execute(
            "SELECT project_id FROM project_deletions WHERE completed=0"
        ).fetchall()
    for row in projects:
        try:
            cleanup_project(database, directory, row[0])
        except (OSError, ValueError):
            # Keep the journal and serve explicit status/retry instead of blocking all projects.
            continue


def delete_project(
    database: Path, directory: Path, project: str, payload: DeleteInput
) -> dict[str, str]:
    lock = DirectoryLock(directory, ".project-cleanup.lock")
    try:
        lock.acquire()
    except BlockingIOError as exc:
        raise TextError(409, "project_busy", "正在恢复或清理项目文件，请稍后重试删除") from exc
    try:
        return _delete_project(database, directory, project, payload)
    finally:
        lock.close()


def _delete_project(
    database: Path, directory: Path, project: str, payload: DeleteInput
) -> dict[str, str]:
    if not payload.acknowledged:
        raise TextError(422, "delete_confirmation", "请明确确认永久删除，操作不可恢复")
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT 1 FROM project_deletions WHERE project_id=?", (project,)
        ).fetchone()
        if prior is None:
            row = connection.execute("SELECT name FROM projects WHERE id=?", (project,)).fetchone()
            if row is None:
                raise TextError(404, "not_found", "项目不存在")
            if row["name"] != payload.confirm_name:
                raise TextError(409, "delete_name_changed", "项目名称不匹配，请重新核对后确认删除")
            if connection.execute(
                "SELECT 1 FROM generation_tasks WHERE project_id=? "
                "AND state IN ('queued','running')",
                (project,),
            ).fetchone():
                raise TextError(
                    409, "project_tasks_active", "项目有排队或运行任务，请取消排队或等待运行完成"
                )
            references = [
                str(UUID(r[0]))
                for r in connection.execute(
                    "SELECT id FROM audio_references WHERE project_id=?", (project,)
                )
            ]
            tasks = [
                str(UUID(r[0]))
                for r in connection.execute(
                    "SELECT id FROM generation_tasks WHERE project_id=?", (project,)
                )
            ]
            paths = [["references", r] for r in references]
            paths += [
                [group, t] for t in tasks for group in ("generation-results", "generation-tmp")
            ]
            paths.append(["exports", project])
            connection.execute(
                "INSERT INTO project_deletions VALUES (?,?,0)", (project, json.dumps(paths))
            )
            connection.execute(
                "INSERT INTO deleted_project_requests SELECT request_id "
                "FROM project_create_requests WHERE project_id=?",
                (project,),
            )
            for table in (
                "project_create_requests",
                "project_archives",
                "paragraph_audio_selections",
                "generation_requests",
                "task_deletions",
                "project_reference_selection",
                "text_confirmations",
                "text_documents",
            ):
                connection.execute(f"DELETE FROM {table} WHERE project_id=?", (project,))
            connection.execute(
                "UPDATE generation_tasks SET retry_of=NULL WHERE project_id=?", (project,)
            )
            connection.execute("DELETE FROM generation_tasks WHERE project_id=?", (project,))
            connection.execute(
                "DELETE FROM reference_transcripts WHERE clip_id IN "
                "(SELECT c.id FROM reference_clips c "
                "JOIN audio_references r ON r.id=c.reference_id WHERE r.project_id=?)",
                (project,),
            )
            connection.execute(
                "DELETE FROM reference_clips WHERE reference_id IN "
                "(SELECT id FROM audio_references WHERE project_id=?)",
                (project,),
            )
            connection.execute("DELETE FROM audio_references WHERE project_id=?", (project,))
            connection.execute("DELETE FROM projects WHERE id=?", (project,))
    try:
        cleanup_project(database, directory, project)
    except (OSError, ValueError) as exc:
        raise TextError(
            503,
            "delete_cleanup_pending",
            "项目已移除，但文件清理未完成；请重试同一删除操作或重启服务后继续清理",
        ) from exc
    return {"project_id": project, "status": "deleted"}
