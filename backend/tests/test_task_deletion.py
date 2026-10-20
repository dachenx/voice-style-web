from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.storage import connect
from backend.app.worker import WorkerSession
from backend.tests.test_exports import payload as export_payload
from backend.tests.test_segments import full_task
from backend.tests.test_versions import choose, edit, history


def remove(client, project, task):
    return client.request(
        "DELETE", f"/api/projects/{project}/tasks/{task}", json={"acknowledged": True}
    )


def test_delete_scrubs_all_files_preserves_assets_and_replay(tmp_path):
    database, project, payload, task = full_task(tmp_path)
    result = tmp_path / "generation-results" / task["id"]
    temporary = tmp_path / "generation-tmp" / task["id"]
    temporary.mkdir(parents=True)
    (temporary / "partial.wav").write_bytes(b"partial")
    references = {
        str(p): p.read_bytes() for p in (tmp_path / "references").rglob("*") if p.is_file()
    }
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        base = f"/api/projects/{project}/tasks"
        before = client.get(f"/api/projects/{project}/text").json()
        assert (
            client.request(
                "DELETE", f"{base}/{task['id']}", json={"acknowledged": False}
            ).status_code
            == 422
        )
        assert result.exists()
        assert remove(client, project, task["id"]).status_code == 200
        assert not result.exists() and not temporary.exists()
        assert remove(client, project, task["id"]).status_code == 200
        assert client.get(base).json()["tasks"] == []
        assert all(not p["versions"] for p in history(client, project)["paragraphs"])
        for suffix in ("", "/audio", "/download"):
            assert client.get(f"{base}/{task['id']}{suffix}").status_code == 410
        assert client.post(base, json=payload).status_code == 410
        assert client.get(f"/api/projects/{project}/text").json() == before
    assert all(__import__("pathlib").Path(p).read_bytes() == data for p, data in references.items())
    with connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM generation_tasks WHERE id=?", (task["id"],)
        ).fetchone()
        assert row["snapshot_json"] == "{}" and row["result_json"] is None
        assert row["error_message"] is None


def test_selected_even_removed_paragraph_and_exported_block(tmp_path):
    database, project, _, task = full_task(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        for index in range(2):
            assert choose(client, project, history(client, project), index).status_code == 200
        assert remove(client, project, task["id"]).json()["error"]["code"] == "task_selected"
        exported = client.post(
            f"/api/projects/{project}/exports", json=export_payload(client, project, pause_ms=0)
        )
        assert exported.status_code == 200, exported.text
        # Remove selections as if they were replaced; the immutable export still protects audio.
        with connect(database) as connection:
            connection.execute(
                "DELETE FROM paragraph_audio_selections WHERE project_id=?", (project,)
            )
        assert remove(client, project, task["id"]).json()["error"]["code"] == "task_exported"
        assert (tmp_path / "generation-results" / task["id"] / "audio.wav").exists()
        manifest = tmp_path / "exports" / project / exported.json()["id"] / "manifest.json"
        manifest.write_text("{}")
        assert remove(client, project, task["id"]).json()["error"]["code"] == "task_export_unknown"


def test_cleanup_fault_restart_retry_and_project_delete(tmp_path):
    database, project, _, task = full_task(tmp_path)
    with patch("backend.app.task_deletion.shutil.rmtree", side_effect=PermissionError):
        with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
            assert remove(client, project, task["id"]).status_code == 503
            listing = client.get(f"/api/projects/{project}/tasks").json()
            assert not listing["tasks"] and listing["deletions"][0]["completed"] == 0
        with TestClient(create_app(tmp_path, worker_enabled=False)) as restarted:
            assert (
                restarted.get(f"/api/projects/{project}/tasks").json()["deletions"][0]["completed"]
                == 0
            )
            with WorkerSession(tmp_path):
                pass  # Recovery skips pending files even while the filesystem remains unavailable.
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert not (tmp_path / "generation-results" / task["id"]).exists()
        assert client.get(f"/api/projects/{project}/tasks").json()["deletions"][0]["completed"] == 1
        name = client.get(f"/api/projects/{project}").json()["name"]
        assert (
            client.request(
                "DELETE",
                f"/api/projects/{project}",
                json={"confirm_name": name, "acknowledged": True},
            ).status_code
            == 200
        )
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM task_deletions").fetchone()[0] == 0


def test_retry_parent_child_and_active_are_not_revived(tmp_path):
    database, project, _, task = full_task(tmp_path)
    with connect(database) as connection:
        connection.execute(
            "UPDATE generation_tasks SET state='failed',result_json=NULL WHERE id=?", (task["id"],)
        )
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        base = f"/api/projects/{project}/tasks"
        child = client.post(f"{base}/{task['id']}/retry", json={"request_id": str(uuid4())}).json()
        assert remove(client, project, child["id"]).json()["error"]["code"] == "task_active"
        assert client.post(f"{base}/{child['id']}/cancel").status_code == 200
        assert remove(client, project, child["id"]).status_code == 200
        assert (
            client.post(f"{base}/{task['id']}/retry", json={"request_id": str(uuid4())}).status_code
            == 410
        )
        assert remove(client, project, task["id"]).status_code == 200
        assert (
            client.post(f"{base}/{task['id']}/retry", json={"request_id": str(uuid4())}).status_code
            == 410
        )


def test_project_exclusive_guard_blocks_selection_export_download_during_cleanup(tmp_path):
    _, project, _, task = full_task(tmp_path)
    entered, release = Event(), Event()
    from backend.app.task_deletion import cleanup_task

    def held(*args):
        entered.set()
        assert release.wait(5)
        return cleanup_task(*args)

    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        catalog = history(client, project)
        with patch("backend.app.task_deletion.cleanup_task", side_effect=held):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(remove, client, project, task["id"])
                assert entered.wait(5)
                try:
                    assert (
                        choose(client, project, catalog).json()["error"]["code"] == "project_busy"
                    )
                    assert (
                        client.post(f"/api/projects/{project}/exports", json={}).json()["error"][
                            "code"
                        ]
                        == "project_busy"
                    )
                    assert (
                        client.get(
                            f"/api/projects/{project}/tasks/{task['id']}/download"
                        ).status_code
                        == 409
                    )
                finally:
                    release.set()
                assert pending.result().status_code == 200


def test_deleted_other_project_cannot_access_and_task_link_does_not_delete_target(tmp_path):
    _, project, _, task = full_task(tmp_path)
    root = tmp_path / "generation-results" / task["id"]
    import shutil

    shutil.rmtree(root)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "keep").write_bytes(b"keep")
    root.symlink_to(protected, target_is_directory=True)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        other = client.post("/api/projects", json={"name": "other"}).json()["id"]
        assert remove(client, other, task["id"]).status_code == 404
        assert remove(client, project, task["id"]).status_code == 200
    assert (protected / "keep").read_bytes() == b"keep"


def test_removed_paragraph_and_damaged_result_cannot_bypass_selected_guard(tmp_path):
    database, project, _, task = full_task(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert choose(client, project, history(client, project)).status_code == 200
        edit(client, project, "delete")
        assert remove(client, project, task["id"]).json()["error"]["code"] == "task_selected"
        with connect(database) as connection:
            connection.execute(
                "UPDATE generation_tasks SET result_json=NULL WHERE id=?", (task["id"],)
            )
        assert (
            remove(client, project, task["id"]).json()["error"]["code"] == "task_selection_unknown"
        )
        assert (tmp_path / "generation-results" / task["id"] / "audio.wav").exists()


def test_inflight_operations_hold_exclusion_through_response_body(tmp_path):
    import asyncio

    from backend.app.project_deletion import ProjectOperations
    from backend.app.storage import migrate

    database = tmp_path / "projects.sqlite3"
    migrate(database)
    project, task_id = str(uuid4()), str(uuid4())

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def endpoint(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"first", "more_body": True})
            started.set()
            await release.wait()
            await send({"type": "http.response.body", "body": b"last", "more_body": False})

        gate = ProjectOperations(endpoint, database)

        async def invoke(method, suffix):
            messages = []

            async def send(message):
                messages.append(message)

            async def receive():
                return {"type": "http.request", "body": b""}

            await gate(
                {"type": "http", "method": method, "path": f"/api/projects/{project}{suffix}"},
                receive,
                send,
            )
            return messages

        for method, suffix in (
            ("GET", f"/tasks/{task_id}/download"),
            ("PUT", f"/paragraphs/{uuid4()}/selection"),
            ("POST", "/exports"),
        ):
            started.clear()
            release.clear()
            operation = asyncio.create_task(invoke(method, suffix))
            await started.wait()
            try:
                assert (await invoke("DELETE", f"/tasks/{task_id}"))[0]["status"] == 409
            finally:
                release.set()
            assert (await operation)[-1]["body"] == b"last"

    asyncio.run(exercise())


def test_process_exit_after_database_commit_recovers_on_restart(tmp_path):
    import subprocess
    import sys

    database, project, _, task = full_task(tmp_path)
    code = """
import os, sys
from pathlib import Path
import backend.app.task_deletion as deletion
def crash(*args):
    os._exit(73)
deletion.cleanup_task = crash
deletion.delete_task(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4])
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(database), str(tmp_path), project, task["id"]],
        timeout=10,
    )
    assert result.returncode == 73
    assert (tmp_path / "generation-results" / task["id"]).exists()
    with connect(database) as connection:
        assert connection.execute("SELECT completed FROM task_deletions").fetchone()[0] == 0
        assert (
            connection.execute("SELECT snapshot_json FROM generation_tasks").fetchone()[0] == "{}"
        )
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert client.get(f"/api/projects/{project}/tasks").json()["tasks"] == []
        assert not (tmp_path / "generation-results" / task["id"]).exists()
