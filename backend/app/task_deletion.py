"""Terminal task erasure, with durable cleanup and replay protection."""

import shutil
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter
from pydantic import StrictBool

from .directory_lock import DirectoryLock
from .exports import export_root, read_export
from .generation import TaskError
from .references import require_project
from .storage import connect
from .text_models import StrictModel
from .versions import candidates


class DeleteTaskInput(StrictModel):
    acknowledged: StrictBool


def cleanup_task(database: Path, directory: Path, task_id: str) -> None:
    task_id = str(UUID(task_id))
    with connect(database) as connection:
        row = connection.execute(
            "SELECT completed FROM task_deletions WHERE task_id=?", (task_id,)
        ).fetchone()
    if row is None or row[0]:
        return
    for group in ("generation-results", "generation-tmp"):
        parent = directory / group
        if parent.is_symlink():
            raise OSError("unsafe task cleanup root")
        path = parent / task_id
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    with connect(database) as connection:
        connection.execute("UPDATE task_deletions SET completed=1 WHERE task_id=?", (task_id,))


def resume_tasks_locked(database: Path, directory: Path) -> None:
    """Caller holds the shared project cleanup lock."""
    with connect(database) as connection:
        rows = connection.execute("SELECT task_id FROM task_deletions WHERE completed=0").fetchall()
    for row in rows:
        try:
            cleanup_task(database, directory, row[0])
        except (OSError, ValueError):
            continue


def check_exports(directory: Path, project: str, task_id: str, version_ids: set[str]) -> None:
    try:
        root = export_root(directory, project)
        if not root.exists():
            return
        for folder in root.iterdir():
            # A partial or damaged export is not evidence of absence of a reference.
            manifest = read_export(folder, project)
            if any(
                segment["version"]["task_id"] == task_id or segment["version"]["id"] in version_ids
                for segment in manifest["segments"]
            ):
                raise TaskError(
                    409,
                    "task_exported",
                    f"该任务音频已被导出 {manifest['id']} 引用，不能删除。"
                    "请先在导出历史中删除相关导出，再重试。",
                )
    except TaskError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TaskError(
            409, "task_export_unknown", "导出记录无法核对，为保护已有导出，暂不能删除任务。"
        ) from exc


def delete_task(database: Path, directory: Path, project: str, task_id: str) -> dict[str, str]:
    lock = DirectoryLock(directory, ".project-cleanup.lock")
    try:
        lock.acquire()
    except BlockingIOError as exc:
        raise TaskError(409, "project_busy", "正在恢复或清理文件，请稍后重试。") from exc
    try:
        with connect(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            require_project(connection, project)
            row = connection.execute(
                "SELECT state FROM generation_tasks WHERE project_id=? AND id=?",
                (project, task_id),
            ).fetchone()
            if row is None:
                raise TaskError(404, "task_not_found", "任务不存在。")
            prior = connection.execute(
                "SELECT 1 FROM task_deletions WHERE task_id=?", (task_id,)
            ).fetchone()
            if prior is None:
                if row["state"] not in {"succeeded", "failed", "canceled"}:
                    raise TaskError(
                        409, "task_active", "排队或运行中的任务不能删除，请取消排队或等待完成。"
                    )
                try:
                    versions, _ = candidates(connection, project)
                except (ValueError, KeyError, TypeError) as exc:
                    raise TaskError(
                        409, "task_selection_unknown", "音频版本记录无法核对，暂不能删除任务。"
                    ) from exc
                identifiers = {v["id"] for v in versions if v["task_id"] == task_id}
                selected = connection.execute(
                    "SELECT version_id,paragraph_id FROM paragraph_audio_selections "
                    "WHERE project_id=?",
                    (project,),
                ).fetchall()
                known = {v["id"] for v in versions}
                if any(r[0] not in known for r in selected):
                    raise TaskError(
                        409, "task_selection_unknown", "已有选用无法对应音频版本，暂不能删除任务。"
                    )
                referenced = [r[1] for r in selected if r[0] in identifiers]
                if referenced:
                    raise TaskError(
                        409,
                        "task_selected",
                        f"该任务音频被段落 {', '.join(referenced)} 选用，"
                        "请先在对应段落改选其他版本后再删除。",
                    )
                check_exports(directory, project, task_id, identifiers)
                connection.execute(
                    "INSERT INTO task_deletions(task_id,project_id) VALUES (?,?)",
                    (task_id, project),
                )
                # Keep only relationship/replay metadata, never the original text or audio data.
                connection.execute(
                    "UPDATE generation_tasks SET snapshot_json='{}',result_json=NULL,"
                    "error_code=NULL,error_message=NULL,phase='deleted',"
                    "completed_paragraphs=0,total_paragraphs=1,state_version=state_version+1 "
                    "WHERE id=?",
                    (task_id,),
                )
        try:
            cleanup_task(database, directory, task_id)
        except (OSError, ValueError) as exc:
            raise TaskError(
                503,
                "task_cleanup_pending",
                "任务记录已删除，音频文件清理尚未完成，请重试清理；重启后也会继续。",
            ) from exc
        return {"task_id": task_id, "state": "deleted"}
    finally:
        lock.close()


def deletion_router(
    database: Path, directory: Path, require_storage: Callable[[], None]
) -> APIRouter:
    routes = APIRouter()

    @routes.delete("/api/projects/{project_id}/tasks/{task_id}")
    def remove(project_id: UUID, task_id: UUID, payload: DeleteTaskInput) -> dict[str, str]:
        require_storage()
        if not payload.acknowledged:
            raise TaskError(422, "delete_confirmation", "请确认删除记录及生成音频，操作不可恢复。")
        return delete_task(database, directory, str(project_id), str(task_id))

    return routes
