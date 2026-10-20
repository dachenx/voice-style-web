from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.generation import GenerationRegistry, TaskInput
from backend.app.main import create_app
from backend.app.storage import connect
from backend.app.tasks import enqueue
from backend.tests.test_exports import payload, setup
from backend.tests.test_task_store import ready_project


def remove(client, path):
    return client.request("DELETE", path, json={"acknowledged": True})


def test_clip_reference_delete_selection_protection_and_retry(tmp_path):
    database, project, body = ready_project(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        refs = client.get(f"/api/projects/{project}/references").json()["references"]
        ref = refs[0]["id"]
        clip = refs[0]["clips"][0]["id"]
        base = f"/api/projects/{project}/references/{ref}"
        assert client.request("DELETE", base, json={"acknowledged": False}).status_code == 422
        other = client.post("/api/projects", json={"name": "other"}).json()["id"]
        assert remove(client, f"/api/projects/{other}/references/{ref}").status_code == 404
        task, _ = enqueue(database, tmp_path, project, TaskInput(**body), GenerationRegistry())
        assert remove(client, base).json()["error"]["code"] == "reference_in_use"
        assert remove(client, base + f"/clips/{clip}").status_code == 409
        assert client.post(f"/api/projects/{project}/tasks/{task['id']}/cancel").status_code == 200
        assert remove(client, f"/api/projects/{project}/tasks/{task['id']}").status_code == 200
        assert remove(client, base + f"/clips/{clip}").status_code == 200
        assert not (tmp_path / "references" / ref / "clips" / f"{clip}.wav").exists()
        assert client.get(f"/api/projects/{project}/references").json()["active_clip_id"] is None
        assert remove(client, base + f"/clips/{clip}").status_code == 200
        with patch("backend.app.asset_deletion.cleanup_asset", side_effect=OSError("disk")):
            assert remove(client, base).json()["error"]["code"] == "asset_cleanup_pending"
        assert client.get(f"/api/projects/{project}/references").json()["references"] == []
    with TestClient(create_app(tmp_path, worker_enabled=False)) as restarted:
        assert not (tmp_path / "references" / ref).exists()
        assert remove(restarted, base).status_code == 200


def test_export_delete_prevents_replay_and_preserves_audio(tmp_path):
    client, project, task = setup(tmp_path)
    base = f"/api/projects/{project}/exports"
    try:
        body = payload(client, project)
        record = client.post(base, json=body).json()
        path = f"{base}/{record['id']}"
        assert remove(client, path).status_code == 200
        assert remove(client, path).status_code == 200
        assert client.get(path + "/audio").status_code == 410
        assert client.post(base, json=body).status_code == 410
        assert client.get(base).json() == []
        assert (tmp_path / "generation-results" / task["id"] / "audio.wav").exists()
        body["request_id"] = str(uuid4())
        assert client.post(base, json=body).status_code == 200
        with connect(tmp_path / "projects.sqlite3") as connection:
            assert (
                connection.execute("SELECT count(*) FROM paragraph_audio_selections").fetchone()[0]
                == 2
            )
    finally:
        client.__exit__(None, None, None)


def test_pending_reference_cleanup_remains_visible_and_service_starts(tmp_path):
    database, project, _ = ready_project(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        ref = client.get(f"/api/projects/{project}/references").json()["references"][0]["id"]
        base = f"/api/projects/{project}/references/{ref}"
        with patch("backend.app.asset_deletion.cleanup_asset", side_effect=OSError("disk")):
            assert remove(client, base).status_code == 503
    # Old orphan cleanup must leave journal-owned paths to the retryable cleaner.
    with patch("backend.app.asset_deletion.cleanup_asset", side_effect=OSError("disk")):
        with TestClient(create_app(tmp_path, worker_enabled=False)) as restarted:
            assert restarted.get("/api/projects").status_code == 200
            assert (tmp_path / "references" / ref).exists()
            pending = restarted.get(f"/api/projects/{project}/asset-deletions").json()
            assert pending == [{"kind": "reference", "asset_id": ref, "reference_id": None}]
    with TestClient(create_app(tmp_path, worker_enabled=False)) as restarted:
        assert restarted.get(f"/api/projects/{project}/asset-deletions").json() == []
        assert not (tmp_path / "references" / ref).exists()


def test_project_delete_includes_pending_reference_files(tmp_path):
    database, project, _ = ready_project(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        ref = client.get(f"/api/projects/{project}/references").json()["references"][0]["id"]
        with patch("backend.app.asset_deletion.cleanup_asset", side_effect=OSError("disk")):
            assert remove(client, f"/api/projects/{project}/references/{ref}").status_code == 503
        assert (tmp_path / "references" / ref).exists()
        deleted = client.request(
            "DELETE",
            f"/api/projects/{project}",
            json={
                "acknowledged": True,
                "confirm_name": "task fixture",
            },
        )
        assert deleted.status_code == 200, deleted.text
        assert not (tmp_path / "references" / ref).exists()
        with connect(database) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM asset_deletions WHERE project_id=?", (project,)
                ).fetchone()[0]
                == 0
            )
