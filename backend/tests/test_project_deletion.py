import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.project_deletion import ProjectOperations
from backend.app.storage import connect
from backend.tests.test_task_store import ready_project
from backend.tests.test_versions import choose, history, prepared


def remove(client, project, name):
    return client.request(
        "DELETE", f"/api/projects/{project}", json={"confirm_name": name, "acknowledged": True}
    )


def test_full_cleanup_replay_and_other_resources_unchanged(tmp_path):
    database, project, _, task = prepared(tmp_path)
    outside = tmp_path / "external-original.wav"
    outside.write_bytes(b"original")
    model = tmp_path / "models" / "keep"
    model.parent.mkdir()
    model.write_bytes(b"model")
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        base = f"/api/projects/{project}"
        name = client.get(base).json()["name"]
        other_body = {"name": "other", "request_id": str(uuid4())}
        other = client.post("/api/projects", json=other_body).json()
        assert choose(client, project, history(client, project)).status_code == 200
        c = history(client, project)
        result = client.post(
            base + "/exports",
            json={
                "request_id": str(uuid4()),
                "adapter_id": "simulated-tone",
                "comparison_token": c["comparison_token"],
                "selections": {
                    p["id"]: p["selection"]["revision"] if p["selection"] else 0
                    for p in c["paragraphs"]
                },
                "paragraph_id": c["paragraphs"][0]["id"],
                "pause_ms": 0,
                "acknowledge_mismatch": False,
            },
        )
        assert result.status_code == 200, result.text
        with connect(database) as db:
            refs = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM audio_references WHERE project_id=?", (project,)
                )
            ]
            request_id = str(uuid4())
            db.execute(
                "INSERT INTO project_create_requests VALUES (?,?,?)", (request_id, name, project)
            )
        assert remove(client, project, "wrong").status_code == 409
        assert (
            client.request(
                "DELETE", base, json={"confirm_name": name, "acknowledged": False}
            ).status_code
            == 422
        )
        assert remove(client, project, name).json()["status"] == "deleted"
        assert remove(client, project, name).status_code == 200
        assert client.get(base).status_code == 410
        assert client.get(base + "/tasks").status_code == 410
        assert (
            client.post("/api/projects", json={"name": name, "request_id": request_id}).status_code
            == 410
        )
        assert client.get(f"/api/projects/{other['id']}").json() == other
        assert client.post("/api/projects", json=other_body).json() == other
        assert all(not (tmp_path / "references" / ref).exists() for ref in refs)
        assert not (tmp_path / "generation-results" / task["id"]).exists()
        assert not (tmp_path / "exports" / project).exists()
        assert outside.read_bytes() == b"original" and model.read_bytes() == b"model"
        with connect(database) as db:
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert not db.execute(
                "SELECT * FROM generation_tasks WHERE project_id=?", (project,)
            ).fetchall()
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert client.get(base + "/deletion-status").json() == {
            "deleted": True,
            "cleanup_complete": True,
        }
        assert remove(client, project, name).status_code == 200


def test_queue_and_running_block_and_transaction_rollback(tmp_path):
    from backend.app.generation import GenerationRegistry, TaskInput
    from backend.app.tasks import enqueue

    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        name = client.get(f"/api/projects/{project}").json()["name"]
        for state in ("queued", "running"):
            with connect(database) as db:
                db.execute("UPDATE generation_tasks SET state=? WHERE id=?", (state, task["id"]))
            assert remove(client, project, name).json()["error"]["code"] == "project_tasks_active"
        with connect(database) as db:
            db.execute("UPDATE generation_tasks SET state='canceled' WHERE id=?", (task["id"],))
            db.execute(
                "CREATE TRIGGER fail_delete BEFORE DELETE ON projects "
                "BEGIN SELECT RAISE(ABORT, 'injected'); END;"
            )
        assert remove(client, project, name).status_code == 503
        assert client.get(f"/api/projects/{project}").status_code == 200
        with connect(database) as db:
            assert not db.execute("SELECT * FROM project_deletions").fetchall()
            assert db.execute("SELECT * FROM generation_tasks WHERE id=?", (task["id"],)).fetchone()
            db.execute("DROP TRIGGER fail_delete")
        assert remove(client, project, name).status_code == 200


def test_cleanup_failure_retry_restart_and_symlink_target_protected(tmp_path, monkeypatch):
    import backend.app.project_deletion as deletion

    database, project, _, _ = prepared(tmp_path)
    real = deletion.shutil.rmtree
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        name = client.get(f"/api/projects/{project}").json()["name"]

        def fail(_):
            raise OSError("injected")

        monkeypatch.setattr(deletion.shutil, "rmtree", fail)
        result = remove(client, project, name)
        assert (
            result.status_code == 503 and result.json()["error"]["code"] == "delete_cleanup_pending"
        )
        assert client.get(f"/api/projects/{project}").status_code == 410
        with connect(database) as db:
            assert db.execute("SELECT completed FROM project_deletions").fetchone()[0] == 0
        monkeypatch.setattr(deletion.shutil, "rmtree", real)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert client.get(f"/api/projects/{project}/deletion-status").json()["cleanup_complete"]
        assert remove(client, project, name).status_code == 200
        p = client.post("/api/projects", json={"name": "symlink"}).json()
        external = tmp_path / "untouched"
        external.mkdir()
        (external / "keep").write_bytes(b"keep")
        exports = tmp_path / "exports"
        exports.mkdir(exist_ok=True)
        (exports / p["id"]).symlink_to(external, target_is_directory=True)
        assert remove(client, p["id"], p["name"]).status_code == 200
        assert (external / "keep").read_bytes() == b"keep"


def test_project_gate_blocks_deletion_during_stream_and_new_operations_during_delete(tmp_path):
    from backend.app.storage import migrate

    database = tmp_path / "projects.sqlite3"
    migrate(database)
    project = str(uuid4())

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def endpoint(scope, receive, send):
            started.set()
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        gate = ProjectOperations(endpoint, database)

        async def invoke(method, path=""):
            messages = []

            async def send(value):
                messages.append(value)

            async def receive():
                return {"type": "http.request", "body": b""}

            await gate(
                {"type": "http", "method": method, "path": f"/api/projects/{project}" + path},
                receive,
                send,
            )
            return messages

        for first, second, suffix in [("POST", "DELETE", "/references"), ("DELETE", "POST", "")]:
            started.clear()
            release.clear()
            task = asyncio.create_task(invoke(first, suffix))
            await started.wait()
            blocked = await invoke(second)
            assert blocked[0]["status"] == 409
            release.set()
            assert (await task)[0]["status"] == 200
        started.clear()
        release.clear()
        task = asyncio.create_task(invoke("GET"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert (await invoke("DELETE"))[0]["status"] == 200

    asyncio.run(exercise())


def test_persistent_cleanup_fault_does_not_disable_other_projects_or_worker(tmp_path, monkeypatch):
    import backend.app.project_deletion as deletion
    from backend.app.worker import WorkerSession

    database, project, _, _ = prepared(tmp_path)
    real = deletion.shutil.rmtree
    with connect(database) as db:
        ref = db.execute(
            "SELECT id FROM audio_references WHERE project_id=?", (project,)
        ).fetchone()[0]

    def fail(path, *args, **kwargs):
        if Path(path).name == ref:
            raise PermissionError("injected persistent fault")
        return real(path, *args, **kwargs)

    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        name = client.get(f"/api/projects/{project}").json()["name"]
        monkeypatch.setattr(deletion.shutil, "rmtree", fail)
        assert remove(client, project, name).status_code == 503
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/projects", json={"name": "still usable"}).status_code == 201
        with WorkerSession(tmp_path):
            pass
        assert not client.get(f"/api/projects/{project}/deletion-status").json()["cleanup_complete"]
        monkeypatch.setattr(deletion.shutil, "rmtree", real)
        assert remove(client, project, name).status_code == 200


def test_worker_cleanup_finishes_before_delete_can_pass(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import backend.app.worker as worker_module
    from backend.app.generation import GenerationRegistry, SimulatedToneAdapter, TaskInput
    from backend.app.tasks import enqueue

    class Failing(SimulatedToneAdapter):
        def generate(self, snapshot, destination, progress):
            destination.write_bytes(b"partial")
            raise RuntimeError("injected failure")

    database, project, payload = ready_project(tmp_path)
    registry = GenerationRegistry([Failing()])
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), registry)
    entered, release = Event(), Event()
    real = worker_module.shutil.rmtree

    def barrier(path, *args, **kwargs):
        if Path(path) == tmp_path / "generation-tmp" / task["id"]:
            entered.set()
            assert release.wait(5)
        return real(path, *args, **kwargs)

    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        name = client.get(f"/api/projects/{project}").json()["name"]
        with worker_module.WorkerSession(tmp_path, registry) as worker:
            monkeypatch.setattr(worker_module.shutil, "rmtree", barrier)
            with ThreadPoolExecutor() as pool:
                future = pool.submit(worker.run_once)
                try:
                    assert entered.wait(5)
                    assert (
                        remove(client, project, name).json()["error"]["code"]
                        == "project_tasks_active"
                    )
                finally:
                    release.set()
                assert future.result(timeout=5)
            assert remove(client, project, name).status_code == 200
            assert not worker.run_once()


def test_process_exit_after_database_commit_recovers_cleanup(tmp_path):
    import subprocess
    import sys

    database, project, _, task = prepared(tmp_path)
    with connect(database) as db:
        name = db.execute("SELECT name FROM projects WHERE id=?", (project,)).fetchone()[0]
    script = """
import os,sys
from pathlib import Path
import backend.app.project_deletion as d
d.cleanup_project=lambda *args: os._exit(77)
d.delete_project(
    Path(sys.argv[1])/'projects.sqlite3', Path(sys.argv[1]), sys.argv[2],
    d.DeleteInput(confirm_name=sys.argv[3], acknowledged=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), project, name],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 77, result.stderr
    assert (tmp_path / "generation-results" / task["id"]).exists()
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert client.get(f"/api/projects/{project}/deletion-status").json()["cleanup_complete"]
        assert not (tmp_path / "generation-results" / task["id"]).exists()
        assert remove(client, project, name).status_code == 200


def test_recovery_lock_blocks_delete_until_cleanup_finishes(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from backend.app.worker import WorkerSession

    _, project, _, _ = prepared(tmp_path)
    entered, release = Event(), Event()
    original = WorkerSession._recover_locked

    def barrier(worker):
        entered.set()
        assert release.wait(5)
        original(worker)

    def recover():
        with WorkerSession(tmp_path):
            pass

    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        name = client.get(f"/api/projects/{project}").json()["name"]
        monkeypatch.setattr(WorkerSession, "_recover_locked", barrier)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(recover)
            try:
                assert entered.wait(5)
                response = remove(client, project, name)
                assert response.status_code == 409
                assert response.json()["error"]["code"] == "project_busy"
                assert client.get(f"/api/projects/{project}").status_code == 200
            finally:
                release.set()
            future.result(timeout=5)
        assert remove(client, project, name).status_code == 200


def test_delete_lock_makes_new_worker_wait_then_recover(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import backend.app.project_deletion as deletion
    from backend.app.directory_lock import DirectoryLock
    from backend.app.worker import WorkerSession

    _, project, _, _ = prepared(tmp_path)
    entered, release, contention, recovered = Event(), Event(), Event(), Event()
    original_cleanup = deletion.cleanup_project
    original_acquire = DirectoryLock.acquire

    def barrier(*args):
        entered.set()
        assert release.wait(5)
        return original_cleanup(*args)

    def acquire(lock):
        try:
            return original_acquire(lock)
        except BlockingIOError:
            if lock.path.name == ".project-cleanup.lock":
                contention.set()
            raise

    def recover():
        with WorkerSession(tmp_path):
            recovered.set()

    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        name = client.get(f"/api/projects/{project}").json()["name"]
        monkeypatch.setattr(deletion, "cleanup_project", barrier)
        monkeypatch.setattr(DirectoryLock, "acquire", acquire)
        with ThreadPoolExecutor() as pool:
            deleting = pool.submit(remove, client, project, name)
            try:
                assert entered.wait(5)
                recovering = pool.submit(recover)
                assert contention.wait(5)
                assert not recovered.is_set()
            finally:
                release.set()
            assert deleting.result(timeout=5).status_code == 200
            recovering.result(timeout=5)
        assert recovered.is_set()
        assert client.get(f"/api/projects/{project}/deletion-status").json()["cleanup_complete"]


def test_pending_export_cleanup_fault_is_isolated_on_restart(tmp_path, monkeypatch):
    import backend.app.project_deletion as deletion

    _, project, _, _ = prepared(tmp_path)
    real = deletion.shutil.rmtree
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        name = client.get(f"/api/projects/{project}").json()["name"]
        other = client.post("/api/projects", json={"name": "other"}).json()["id"]
        pending = tmp_path / "exports" / project / ".pending-interrupted"
        pending.mkdir(parents=True)
        (pending / "partial.wav").write_bytes(b"partial")
        other_pending = tmp_path / "exports" / other / ".pending-interrupted"
        other_pending.mkdir(parents=True)

        def fail(path, *args, **kwargs):
            if Path(path) in (pending, pending.parent):
                raise PermissionError("injected export cleanup fault")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(deletion.shutil, "rmtree", fail)
        assert remove(client, project, name).json()["error"]["code"] == "delete_cleanup_pending"
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get(f"/api/projects/{other}").status_code == 200
        assert client.post("/api/projects", json={"name": "still usable"}).status_code == 201
        assert pending.exists()
        assert not other_pending.exists()
        assert not client.get(f"/api/projects/{project}/deletion-status").json()["cleanup_complete"]
        monkeypatch.setattr(deletion.shutil, "rmtree", real)
        assert remove(client, project, name).status_code == 200
        assert not pending.parent.exists()
