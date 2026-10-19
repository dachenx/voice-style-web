"""Archive is reversible organization, creation requests are durable identities."""

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.storage import connect, migrate


def test_create_replay_conflict_rename_and_restart(tmp_path: Path) -> None:
    body = {"name": "  同名项目  ", "request_id": str(uuid4())}
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: client.post("/api/projects", json=body), range(4)))
        assert all(r.status_code == 201 for r in results)
        project = results[0].json()
        assert all(r.json()["id"] == project["id"] for r in results)
        url = f"/api/projects/{project['id']}"
        client.patch(url, json={"name": "已重命名"})
        assert client.post("/api/projects", json=body).json()["name"] == "已重命名"
        assert client.post("/api/projects", json={**body, "name": "冲突"}).status_code == 409
        assert client.post("/api/projects", json={"name": "同名项目"}).json()["id"] != project["id"]
        assert len(client.get("/api/projects").json()["projects"]) == 2
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert client.post("/api/projects", json=body).json()["id"] == project["id"]
        assert len(client.get("/api/projects").json()["projects"]) == 2


def test_archive_restore_preserves_contents_and_old_links(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        project = client.post("/api/projects", json={"name": "保留资料"}).json()
        url = f"/api/projects/{project['id']}"
        doc = {
            "expected_revision": 0,
            "source_language": "zh-CN",
            "target_language": "wuhan",
            "register": "",
            "terms": "",
            "paragraphs": [],
        }
        assert client.put(url + "/text", json=doc).status_code == 200
        before = client.get(url + "/text").json()
        for _ in range(2):
            assert client.put(url + "/archive", json={"archived": True}).json()["archived"] == 1
        assert client.get("/api/projects").json()["projects"] == []
        assert (
            client.get("/api/projects?archived=true").json()["projects"][0]["id"] == project["id"]
        )
        assert client.get(url).json()["id"] == project["id"]
        assert client.get(url + "/text").json() == before
        assert client.put(url + "/archive", json={"archived": "false"}).status_code == 422
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert (
            client.get("/api/projects?archived=true").json()["projects"][0]["id"] == project["id"]
        )
        for _ in range(2):
            assert client.put(url + "/archive", json={"archived": False}).json()["archived"] == 0
        assert client.get(url + "/text").json() == before
        assert len(client.get("/api/projects").json()["projects"]) == 1
        assert (
            client.put(f"/api/projects/{uuid4()}/archive", json={"archived": True}).status_code
            == 404
        )


def test_upgrade_from_six_preserves_rows(tmp_path: Path) -> None:
    old = tmp_path / "old-migrations"
    old.mkdir()
    migrations = Path("backend/app/migrations")
    for source in sorted(migrations.glob("*.sql"))[:6]:
        shutil.copyfile(source, old / source.name)
    db = tmp_path / "projects.sqlite3"
    migrate(db, old)
    with connect(db) as connection:
        connection.execute("INSERT INTO projects VALUES ('old', 'preserved', 'now', 'now')")
    migrate(db)
    migrate(db)
    with connect(db) as connection:
        assert tuple(connection.execute("SELECT * FROM projects").fetchone()) == (
            "old",
            "preserved",
            "now",
            "now",
        )
        assert connection.execute("SELECT COUNT(*) FROM project_archives").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM project_create_requests").fetchone()[0] == 0
        count = connection.execute("SELECT COUNT(*) FROM abandoned_project_requests").fetchone()[0]
        assert count == 0


def test_archived_queued_task_finishes_and_restore_keeps_audio_selection(tmp_path: Path) -> None:
    from backend.app.generation import GenerationRegistry, TaskInput
    from backend.app.tasks import enqueue
    from backend.app.worker import WorkerSession
    from backend.tests.test_task_store import ready_project
    from backend.tests.test_versions import choose, history

    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    base = f"/api/projects/{project}"
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        before = client.get(base + "/text").json()
        assert client.put(base + "/archive", json={"archived": True}).status_code == 200
        with WorkerSession(tmp_path) as worker:
            worker.run_once()
        assert client.get(base + f"/tasks/{task['id']}").json()["state"] == "succeeded"
        catalog = history(client, project)
        assert choose(client, project, catalog).status_code == 200
        selected = history(client, project)["paragraphs"][0]["selection"]
        audio = client.get(base + f"/tasks/{task['id']}/audio")
        assert audio.status_code == 200 and len(audio.content) > 44
        assert client.put(base + "/archive", json={"archived": False}).status_code == 200
        assert client.get(base + "/text").json() == before
        assert history(client, project)["paragraphs"][0]["selection"] == selected
        assert client.get(base + f"/tasks/{task['id']}/audio").content == audio.content


def test_resolve_abandoned_persists_and_late_create_is_rejected(tmp_path: Path) -> None:
    body = {"name": "原请求", "request_id": str(uuid4())}
    url = f"/api/project-creation-requests/{body['request_id']}/resolve"
    for _ in range(2):
        with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
            assert client.post(url, json={"name": " 原请求 "}).json() == {"status": "abandoned"}
            assert client.post(url, json={"name": "其他"}).status_code == 409
            late = client.post("/api/projects", json=body)
            assert late.status_code == 410
            assert late.json()["error"]["code"] == "project_request_abandoned"
            assert client.get("/api/projects").json()["projects"] == []
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert client.post("/api/projects", json={"name": body["name"]}).status_code == 201


def test_resolve_created_preserves_current_project_and_deleted_state(tmp_path: Path) -> None:
    body = {"name": "原请求", "request_id": str(uuid4())}
    url = f"/api/project-creation-requests/{body['request_id']}/resolve"
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        project = client.post("/api/projects", json=body).json()
        base = f"/api/projects/{project['id']}"
        client.patch(base, json={"name": "重命名"})
        client.put(base + "/archive", json={"archived": True})
        current = client.get(base).json()
        for _ in range(2):
            assert client.post(url, json={"name": "原请求"}).json() == {
                "status": "created", "project": current
            }
        assert client.post(url, json={"name": "重命名"}).status_code == 409
        assert client.get(base).json() == current
        assert len(client.get("/api/projects?archived=true").json()["projects"]) == 1
        removed = client.request(
            "DELETE", base, json={"confirm_name": "重命名", "acknowledged": True}
        )
        assert removed.status_code == 200
        assert client.post(url, json={"name": "原请求"}).json() == {"status": "deleted"}
        assert client.post("/api/projects", json=body).status_code == 410


def test_resolve_and_create_concurrent_have_one_durable_outcome(tmp_path: Path) -> None:
    from threading import Barrier

    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        for _ in range(12):
            body = {"name": "竞争", "request_id": str(uuid4())}
            url = f"/api/project-creation-requests/{body['request_id']}/resolve"
            barrier = Barrier(2)

            def create(barrier=barrier, body=body):
                barrier.wait()
                return client.post("/api/projects", json=body)

            def resolve(barrier=barrier, body=body, url=url):
                barrier.wait()
                return client.post(url, json={"name": body["name"]})

            with ThreadPoolExecutor(max_workers=2) as pool:
                creating = pool.submit(create)
                resolving = pool.submit(resolve)
                created, resolved = creating.result(), resolving.result()
            assert resolved.status_code == 200
            if resolved.json()["status"] == "created":
                assert created.status_code == 201
                assert resolved.json()["project"]["id"] == created.json()["id"]
            else:
                assert resolved.json() == {"status": "abandoned"}
                assert created.status_code == 410
            assert client.post(url, json={"name": body["name"]}).json() == resolved.json()
