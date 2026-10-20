import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.generation import GenerationRegistry, TaskInput
from backend.app.main import create_app
from backend.app.storage import connect, migrate
from backend.app.tasks import enqueue
from backend.app.worker import WorkerSession
from backend.tests.test_model_bridge import setup_real
from backend.tests.test_task_store import ready_project


def prepared(directory: Path, *, full: bool = False) -> tuple[Any, ...]:
    database, project, payload = ready_project(directory)
    if full:
        payload.update(mode="full", paragraph_id=None)
    task, _ = enqueue(database, directory, project, TaskInput(**payload), GenerationRegistry())
    # This fixture remains an actual M04/M05-format task after the new output contract arrives.
    task["snapshot"].pop("output_contract", None)
    with connect(database) as connection:
        connection.execute(
            "UPDATE generation_tasks SET snapshot_json=? WHERE id=?",
            (json.dumps(task["snapshot"]), task["id"]),
        )
    with WorkerSession(directory) as worker:
        worker.run_once()
    return database, project, payload, task


def history(client: TestClient, project: str, adapter: str = "simulated-tone") -> dict[str, Any]:
    response = client.get(f"/api/projects/{project}/paragraph-versions?adapter_id={adapter}")
    assert response.status_code == 200, response.text
    return response.json()


def choose(
    client: TestClient,
    project: str,
    catalog: dict[str, Any],
    index: int = 0,
    *,
    acknowledge: bool = False,
) -> Any:
    paragraph = catalog["paragraphs"][index]
    return client.put(
        f"/api/projects/{project}/paragraphs/{paragraph['id']}/selection",
        json={
            "version_id": paragraph["versions"][0]["id"],
            "expected_revision": paragraph["selection"]["revision"]
            if paragraph["selection"]
            else 0,
            "adapter_id": "simulated-tone",
            "comparison_token": catalog["comparison_token"],
            "acknowledge_mismatch": acknowledge,
        },
    )


def edit(client: TestClient, project: str, action: str) -> None:
    doc = client.get(f"/api/projects/{project}/text").json()
    paragraphs = doc["paragraphs"]
    if action == "text":
        paragraphs[0]["draft_text"] += " changed"
    elif action == "note":
        paragraphs[0]["pronunciation_note"] += " only metadata"
    elif action == "delete":
        paragraphs.pop(0)
    elif action == "reorder":
        paragraphs.reverse()
    response = client.put(
        f"/api/projects/{project}/text",
        json={
            "expected_revision": doc["revision"],
            "source_language": doc["source_language"],
            "target_language": doc["target_language"],
            "register": doc["register"],
            "terms": doc["terms"],
            "paragraphs": paragraphs,
        },
    )
    assert response.status_code == 200, response.text


def test_legacy_preview_read_only_identity_selection_audio_and_restart(tmp_path: Path) -> None:
    database, project, _, task = prepared(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        first = history(client, project)
        version = first["paragraphs"][0]["versions"][0]
        assert version["legacy"] and version["comparison"] == "matching"
        assert version["available"] and version["task_id"] == task["id"]
        before = database.read_bytes()
        assert history(client, project) == first
        assert database.read_bytes() == before
        audio = client.get(f"/api/projects/{project}/paragraph-versions/{version['id']}/audio")
        assert audio.status_code == 200
        assert (
            audio.content
            == (tmp_path / "generation-results" / task["id"] / "audio.wav").read_bytes()
        )
        assert choose(client, project, first).status_code == 200
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        paragraph = history(client, project)["paragraphs"][0]
        assert paragraph["selection"]["version_id"] == version["id"]
        assert paragraph["versions"][0]["id"] == version["id"]


def test_legacy_full_has_no_invented_segments(tmp_path: Path) -> None:
    _, project, _, task = prepared(tmp_path, full=True)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        catalog = history(client, project)
        assert catalog["legacy_full_count"] == 1
        assert all(not p["versions"] for p in catalog["paragraphs"])
        assert client.get(f"/api/projects/{project}/tasks/{task['id']}/audio").status_code == 200


def test_revisions_unused_notes_reorder_do_not_stale_but_actual_text_does(tmp_path: Path) -> None:
    _, project, _, _ = prepared(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        initial = history(client, project)
        assert choose(client, project, initial).status_code == 200
        edit(client, project, "note")
        catalog = history(client, project)
        assert not catalog["confirmation_current"]
        assert catalog["paragraphs"][0]["versions"][0]["comparison"] == "matching"
        edit(client, project, "text")
        catalog = history(client, project)
        version = catalog["paragraphs"][0]["versions"][0]
        assert version["stale_reasons"] == ["本段草稿已改变"]
        assert choose(client, project, catalog).status_code == 409
        assert choose(client, project, catalog, acknowledge=True).status_code == 200
        assert history(client, project)["paragraphs"][0]["versions"][0]["comparison"] == "stale"
        edit(client, project, "reorder")
        reordered = history(client, project)["paragraphs"][1]
        assert reordered["selection"]["version_id"] == version["id"]
        assert reordered["versions"][0]["stale_reasons"] == ["本段草稿已改变"]


def test_selection_cas_and_input_change_are_not_silent_overwrites(tmp_path: Path) -> None:
    _, project, _, _ = prepared(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        initial = history(client, project)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: choose(client, project, initial), range(2)))
        assert sorted(r.status_code for r in responses) == [200, 409]
        fresh = history(client, project)
        edit(client, project, "text")
        response = choose(client, project, fresh)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "comparison_changed"
        assert history(client, project)["paragraphs"][0]["selection"]["revision"] == 1


def test_missing_audio_deleted_paragraph_and_other_project_keep_history(tmp_path: Path) -> None:
    _, project, _, task = prepared(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        catalog = history(client, project)
        version = catalog["paragraphs"][0]["versions"][0]
        assert choose(client, project, catalog).status_code == 200
        other = client.post("/api/projects", json={"name": "other"}).json()["id"]
        route = f"/api/projects/{other}/paragraph-versions/{version['id']}/audio"
        assert client.get(route).status_code == 404
        path = tmp_path / "generation-results" / task["id"] / "audio.wav"
        path.write_bytes(b"damaged")
        catalog = history(client, project)
        assert not catalog["paragraphs"][0]["versions"][0]["available"]
        assert choose(client, project, catalog).status_code == 409
        assert catalog["paragraphs"][0]["selection"]["version_id"] == version["id"]
        edit(client, project, "delete")
        catalog = history(client, project)
        assert catalog["archived_versions"][0]["id"] == version["id"]
        assert catalog["archived_versions"][0]["comparison"] == "stale"
        assert client.get(f"/api/projects/{project}/tasks/{task['id']}").status_code == 200


def test_reference_change_marks_existing_candidate_without_deleting_selection(
    tmp_path: Path,
) -> None:
    database, project, _, _ = prepared(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        catalog = history(client, project)
        assert choose(client, project, catalog).status_code == 200
        with connect(database) as connection:
            old = connection.execute("SELECT * FROM reference_clips").fetchone()
            identifier = str(uuid4())
            connection.execute(
                "INSERT INTO reference_clips VALUES (?,?,0,1000,'now')",
                (identifier, old["reference_id"]),
            )
            connection.execute("UPDATE project_reference_selection SET clip_id=?", (identifier,))
        original = tmp_path / "references" / old["reference_id"] / "clips" / f"{old['id']}.wav"
        original.with_name(f"{identifier}.wav").write_bytes(original.read_bytes())
        catalog = history(client, project)
        version = catalog["paragraphs"][0]["versions"][0]
        assert version["stale_reasons"] == ["当前参考片段已改变"]
        assert version["available"]
        assert catalog["paragraphs"][0]["selection"]["revision"] == 1


@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_real_dependency_unknown_recovery_and_actual_transcript_change(
    tmp_path: Path, damage: str
) -> None:
    client, directory, _, project, task, registry, _, _, location = setup_real(tmp_path)
    try:
        with WorkerSession(directory, registry) as worker:
            assert worker.run_once()
        assert (
            history(client, project, "cosyvoice3-local")["paragraphs"][0]["versions"][0][
                "comparison"
            ]
            == "matching"
        )
    finally:
        client.__exit__(None, None, None)
    original = location.read_bytes()
    if damage == "missing":
        location.unlink()
    else:
        location.write_text("invalid config")
    # Configuration is loaded at service startup; restart to load the changed pointer.
    with TestClient(
        create_app(directory, worker_enabled=False, model_config_path=location)
    ) as client:

        def current() -> dict[str, Any]:
            return history(client, project, "cosyvoice3-local")["paragraphs"][0]["versions"][0]

        value = current()
        assert value["comparison"] == "unknown"
        assert not value["comparison_complete"] and value["stale_reasons"] == []
        edit(client, project, "text")
        value = current()
        assert value["comparison"] == "stale" and not value["comparison_complete"]
        assert value["stale_reasons"] == ["本段草稿已改变"]
    location.write_bytes(original)
    with TestClient(
        create_app(directory, worker_enabled=False, model_config_path=location)
    ) as client:
        assert current()["comparison_complete"]
        reference = task["snapshot"]["reference"]
        base = f"/api/projects/{project}/references/{reference['id']}/clips"
        base += f"/{reference['clip']['id']}/transcript"
        for text, expected in ((reference["transcript"]["text"], False), ("different words", True)):
            before = client.get(base).json()
            response = client.put(
                base,
                json={
                    "expected_revision": before["revision"],
                    "expected_clip_sha256": before["clip_sha256"],
                    "text": text,
                },
            )
            assert response.status_code == 200
            assert ("参考准确原话已改变" in current()["stale_reasons"]) == expected


def test_m05_upgrade_preserves_literal_task_and_adds_empty_selection_table(tmp_path: Path) -> None:
    database, _, _, _ = prepared(tmp_path)
    with connect(database) as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM generation_tasks")]
        connection.execute("DROP TABLE paragraph_audio_selections")
        connection.execute("DROP TABLE project_deletions")
        connection.execute("DROP TABLE deleted_project_requests")
        connection.execute("DROP TABLE project_archives")
        connection.execute("DROP TABLE project_create_requests")
        connection.execute("DROP TABLE abandoned_project_requests")
        connection.execute("DROP TABLE task_deletions")
        connection.execute("DELETE FROM schema_migrations WHERE version>=6")
    migrate(database)
    migrate(database)
    with connect(database) as connection:
        assert before == [
            tuple(row) for row in connection.execute("SELECT * FROM generation_tasks")
        ]
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 10
        assert not connection.execute("SELECT * FROM paragraph_audio_selections").fetchall()
