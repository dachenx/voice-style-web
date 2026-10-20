"""App-owned reference and export cleanup with durable replay protection."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter

from .directory_lock import DirectoryLock
from .generation import TaskError
from .references import require_project
from .storage import connect
from .task_deletion import DeleteTaskInput


def cleanup_asset(database: Path, directory: Path, row: Any) -> None:
    project, kind, identifier = row["project_id"], row["kind"], row["asset_id"]
    UUID(project)
    UUID(identifier)
    if kind == "export":
        parts = ["exports", project, identifier]
    elif kind == "reference":
        parts = ["references", identifier]
    elif kind == "clip":
        reference = str(UUID(row["reference_id"]))
        parts = ["references", reference, "clips", f"{identifier}.wav"]
    else:
        raise ValueError("invalid asset kind")
    path = directory
    for part in parts[:-1]:
        path = path / part
        if path.is_symlink():
            raise OSError("unsafe cleanup parent")
    path = path / parts[-1]
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    with connect(database) as connection:
        connection.execute(
            "UPDATE asset_deletions SET completed=1 WHERE project_id=? AND kind=? AND asset_id=?",
            (project, kind, identifier),
        )


def resume_assets(database: Path, directory: Path) -> None:
    with connect(database) as connection:
        rows = connection.execute("SELECT * FROM asset_deletions WHERE completed=0").fetchall()
    for row in rows:
        try:
            cleanup_asset(database, directory, row)
        except (OSError, ValueError):
            continue


def remove_asset(
    database: Path,
    directory: Path,
    project: str,
    kind: str,
    identifier: str,
    reference: str | None = None,
) -> dict[str, str]:
    lock = DirectoryLock(directory, ".project-cleanup.lock")
    try:
        lock.acquire()
    except BlockingIOError as exc:
        raise TaskError(409, "project_busy", "正在清理文件，请稍后重试。") from exc
    try:
        with connect(database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            require_project(connection, project)
            prior = connection.execute(
                "SELECT * FROM asset_deletions WHERE project_id=? AND kind=? AND asset_id=?",
                (project, kind, identifier),
            ).fetchone()
            if prior is None:
                if kind == "export":
                    root = directory / "exports" / project
                    if root.is_symlink() or not (root / identifier).exists():
                        raise TaskError(404, "export_not_found", "导出不存在。")
                else:
                    ref = identifier if kind == "reference" else reference
                    if not connection.execute(
                        "SELECT 1 FROM audio_references WHERE id=? AND project_id=?",
                        (ref, project),
                    ).fetchone():
                        raise TaskError(404, "reference_not_found", "参考素材不存在。")
                    clips = [
                        r[0]
                        for r in connection.execute(
                            "SELECT id FROM reference_clips WHERE reference_id=?", (ref,)
                        )
                    ]
                    if kind == "clip" and identifier not in clips:
                        raise TaskError(404, "clip_not_found", "参考片段不存在。")
                    affected = set(clips) if kind == "reference" else {identifier}
                    for task in connection.execute(
                        "SELECT snapshot_json FROM generation_tasks WHERE project_id=? "
                        "AND id NOT IN (SELECT task_id FROM task_deletions)",
                        (project,),
                    ):
                        try:
                            snapshot = json.loads(task[0])
                            used = snapshot["reference"]
                            if snapshot.get("reference_source") != "persona" and (
                                (kind == "reference" and used["id"] == ref)
                                or used["clip"]["id"] in affected
                            ):
                                raise TaskError(
                                    409,
                                    "reference_in_use",
                                    "素材被任务快照引用，请先删除相关任务及其引用后再清理。",
                                )
                        except (KeyError, TypeError, ValueError) as exc:
                            raise TaskError(
                                409, "reference_unknown", "无法核对任务引用，暂不能删除。"
                            ) from exc
                    for clip in affected:
                        connection.execute(
                            "DELETE FROM project_reference_selection WHERE clip_id=?", (clip,)
                        )
                        connection.execute(
                            "DELETE FROM reference_transcripts WHERE clip_id=?", (clip,)
                        )
                        connection.execute("DELETE FROM reference_clips WHERE id=?", (clip,))
                    if kind == "reference":
                        connection.execute("DELETE FROM audio_references WHERE id=?", (ref,))
                connection.execute(
                    "INSERT INTO asset_deletions(project_id,kind,asset_id,reference_id) "
                    "VALUES (?,?,?,?)",
                    (project, kind, identifier, reference),
                )
            row = connection.execute(
                "SELECT * FROM asset_deletions WHERE project_id=? AND kind=? AND asset_id=?",
                (project, kind, identifier),
            ).fetchone()
        try:
            cleanup_asset(database, directory, row)
        except (OSError, ValueError) as exc:
            raise TaskError(
                503,
                "asset_cleanup_pending",
                "删除已登记，文件尚未清理完成，请重试；重启会继续清理。",
            ) from exc
        return {"state": "deleted", "id": identifier}
    finally:
        lock.close()


def asset_router(database: Path, directory: Path, require_storage: Callable[[], None]) -> APIRouter:
    routes = APIRouter(prefix="/api/projects/{project_id}")

    @routes.get("/asset-deletions")
    def pending(project_id: UUID) -> list[dict[str, Any]]:
        require_storage()
        with connect(database) as connection:
            require_project(connection, str(project_id))
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT kind,asset_id,reference_id FROM asset_deletions "
                    "WHERE project_id=? AND completed=0",
                    (str(project_id),),
                )
            ]

    def checked(
        project: UUID,
        kind: str,
        identifier: UUID,
        payload: DeleteTaskInput,
        reference: UUID | None = None,
    ) -> dict[str, str]:
        require_storage()
        if not payload.acknowledged:
            raise TaskError(422, "delete_confirmation", "请确认永久删除，操作不可恢复。")
        return remove_asset(
            database,
            directory,
            str(project),
            kind,
            str(identifier),
            str(reference) if reference else None,
        )

    @routes.delete("/references/{reference_id}")
    def reference_delete(
        project_id: UUID, reference_id: UUID, payload: DeleteTaskInput
    ) -> dict[str, str]:
        return checked(project_id, "reference", reference_id, payload)

    @routes.delete("/references/{reference_id}/clips/{clip_id}")
    def clip_delete(
        project_id: UUID, reference_id: UUID, clip_id: UUID, payload: DeleteTaskInput
    ) -> dict[str, str]:
        return checked(project_id, "clip", clip_id, payload, reference_id)

    @routes.delete("/exports/{export_id}")
    def export_delete(
        project_id: UUID, export_id: UUID, payload: DeleteTaskInput
    ) -> dict[str, str]:
        return checked(project_id, "export", export_id, payload)

    return routes
