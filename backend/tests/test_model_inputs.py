import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.generation import GenerationRegistry, TaskInput
from backend.app.main import create_app
from backend.app.model_config import (
    REQUIRED_MODEL_FILES,
    ModelConfigError,
    build_config,
    load_config,
    save_config,
    validate_config,
)
from backend.app.storage import connect, migrate
from backend.app.tasks import canonical, enqueue, file_digest
from backend.app.worker import WorkerSession
from backend.tests.test_task_store import ready_project

M04_CAPABILITY = {
    "id": "simulated-tone",
    "version": "1",
    "label": "模拟测试音",
    "is_simulated": True,
    "output_kind": "synthetic-tone",
    "modes": ["preview", "full"],
    "supported_parameters": [],
}


def fixture_model(directory: Path) -> tuple[Path, dict[str, Any]]:
    # Synthetic files exercise configuration transport, never model loading or voice quality.
    repo = directory / "fixture-repo"
    (repo / "cosyvoice/cli").mkdir(parents=True)
    (repo / "cosyvoice/cli/cosyvoice.py").write_text("# fixture only\n")
    (repo / "third_party/Matcha-TTS/matcha").mkdir(parents=True)
    (repo / "third_party/Matcha-TTS/matcha/__init__.py").write_text("# fixture only\n")
    model = repo / "model"
    for name in REQUIRED_MODEL_FILES:
        path = model / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("fixture: " + name).encode())
    config = build_config(Path(sys.executable), repo, model)
    location = directory / "private/model-runtime.json"
    save_config(config, location)
    return location, config


def transcript_base(project: str, task: dict[str, Any]) -> str:
    reference = task["snapshot"]["reference"]
    return (
        f"/api/projects/{project}/references/{reference['id']}"
        f"/clips/{reference['clip']['id']}/transcript"
    )


def test_transcript_versions_cas_isolation_and_restart(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path / "data")
    task, _ = enqueue(
        database, tmp_path / "data", project, TaskInput(**payload), GenerationRegistry()
    )
    base = transcript_base(project, task)
    with TestClient(create_app(tmp_path / "data", worker_enabled=False)) as client:
        initial = client.get(base)
        assert initial.status_code == 200
        value = initial.json()
        assert value["revision"] == 0 and value["text"] == ""
        body = {
            "expected_revision": 0,
            "expected_clip_sha256": value["clip_sha256"],
            "text": "准确原话128元",
        }
        first = client.put(base, json=body)
        assert first.status_code == 200
        assert first.json()["revision"] == 1
        assert client.put(base, json=body).status_code == 409
        second = client.put(base, json={**body, "expected_revision": 1, "text": ""})
        assert second.status_code == 200 and second.json()["revision"] == 2
        assert second.json()["created_at"] == first.json()["created_at"]
        wrong = base.replace(project, str(uuid4()))
        assert client.get(wrong).status_code == 404
        assert client.put(wrong, json=body).status_code == 404
        assert (
            client.put(
                base, json={**body, "expected_revision": 2, "expected_clip_sha256": "0" * 64}
            ).status_code
            == 409
        )
    with TestClient(create_app(tmp_path / "data", worker_enabled=False)) as client:
        assert client.get(base).json() == second.json()
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reference_transcripts").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT text FROM reference_transcripts WHERE revision=1"
            ).fetchone()[0]
            == "准确原话128元"
        )


@pytest.mark.parametrize("case", ["boolean", "float", "control", "too-long", "wrong-sha", "extra"])
def test_transcript_validation(tmp_path: Path, case: str) -> None:
    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    base = transcript_base(project, task)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        value = client.get(base).json()
        body: dict[str, Any] = {
            "expected_revision": 0,
            "expected_clip_sha256": value["clip_sha256"],
            "text": "words",
        }
        if case in ("boolean", "float"):
            body["expected_revision"] = True if case == "boolean" else 0.0
        elif case == "control":
            body["text"] = "bad\x00"
        elif case == "too-long":
            body["text"] = "x" * 2001
        elif case == "wrong-sha":
            body["expected_clip_sha256"] = "invalid"
        else:
            body["extra"] = True
        assert client.put(base, json=body).status_code == 422
        assert client.get(base).json()["revision"] == 0


def test_transcript_real_transaction_concurrent_cas(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    base = transcript_base(project, task)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        value = client.get(base).json()
        body = {
            "expected_revision": 0,
            "expected_clip_sha256": value["clip_sha256"],
            "text": "words",
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.put, base, json=body) for _ in range(2)]
            assert sorted(item.result(timeout=10).status_code for item in futures) == [200, 409]


def test_transcript_rejects_changed_audio_bytes(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    base = transcript_base(project, task)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        value = client.get(base).json()
        body = {
            "expected_revision": 0,
            "expected_clip_sha256": value["clip_sha256"],
            "text": "words",
        }
        assert client.put(base, json=body).status_code == 200
        reference = task["snapshot"]["reference"]
        path = (
            tmp_path / "references" / reference["id"] / "clips" / f"{reference['clip']['id']}.wav"
        )
        changed = bytearray(path.read_bytes())
        changed[-2:] = b"\x02\x00"
        path.write_bytes(changed)
        assert client.get(base).status_code == 409
        assert client.put(base, json={**body, "expected_revision": 1}).status_code == 409
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reference_transcripts").fetchone()[0] == 1


def test_private_config_hash_health_and_no_model_loading_claim(tmp_path: Path) -> None:
    location, config = fixture_model(tmp_path)
    assert load_config(location) == config
    validate_config(config, full_hash=True)
    with TestClient(
        create_app(tmp_path / "data", worker_enabled=False, model_config_path=location)
    ) as client:
        capabilities = client.get("/api/generation-adapters").json()["adapters"]
        assert capabilities[0] == M04_CAPABILITY
        real = capabilities[1]
        assert (
            not real["is_simulated"] and real["config_fingerprint"] == config["config_fingerprint"]
        )
        assert "repository" not in real and str(tmp_path) not in json.dumps(capabilities)
        health = client.get("/api/generation-adapters/cosyvoice3-local/health").json()
        assert health["configuration_valid"] and health["configured"]
        assert health["execution_available"] and not health["model_loaded"]
        assert str(tmp_path) not in json.dumps(health)
    changed = Path(config["model_directory"]) / "llm.pt"
    changed.write_bytes(b"changed")
    with pytest.raises(ModelConfigError):
        validate_config(config)
    with TestClient(
        create_app(tmp_path / "data", worker_enabled=False, model_config_path=location)
    ) as client:
        assert not client.get("/api/generation-adapters/cosyvoice3-local/health").json()[
            "configuration_valid"
        ]


@pytest.mark.parametrize("case", ["absent", "broken-json", "changed-config"])
def test_bad_config_preserves_simulated_adapter(tmp_path: Path, case: str) -> None:
    location = tmp_path / "model-runtime.json"
    if case == "broken-json":
        location.write_text("{")
    elif case == "changed-config":
        location, value = fixture_model(tmp_path)
        archive = location.parent / "model-configs" / f"{value['config_fingerprint']}.json"
        archive.write_text(archive.read_text().replace('"threads":6', '"threads":7'))
    with TestClient(
        create_app(tmp_path / "data", worker_enabled=False, model_config_path=location)
    ) as client:
        caps = client.get("/api/generation-adapters").json()["adapters"]
        assert caps[0] == M04_CAPABILITY
        assert client.get("/api/generation-adapters/simulated-tone/health").json()[
            "execution_available"
        ]
        assert not client.get("/api/generation-adapters/cosyvoice3-local/health").json()[
            "configuration_valid"
        ]


def test_real_enqueue_freezes_transcript_and_private_model_identity(tmp_path: Path) -> None:
    location, config = fixture_model(tmp_path / "model-fixture")
    directory = tmp_path / "data"
    database, project, payload = ready_project(directory)
    simulated, _ = enqueue(database, directory, project, TaskInput(**payload), GenerationRegistry())
    base = transcript_base(project, simulated)
    with TestClient(
        create_app(directory, worker_enabled=False, model_config_path=location)
    ) as client:
        value = client.get(base).json()
        body = {
            "expected_revision": 0,
            "expected_clip_sha256": value["clip_sha256"],
            "text": "准确原话128元",
        }
        transcript = client.put(base, json=body).json()
        real_input = {
            **payload,
            "request_id": str(uuid4()),
            "adapter_id": "cosyvoice3-local",
            "expected_transcript_revision": 1,
            "expected_adapter_version": "1",
            "expected_config_fingerprint": config["config_fingerprint"],
        }
        response = client.post(f"/api/projects/{project}/tasks", json=real_input)
        assert response.status_code == 201, response.text
        task = response.json()
        assert not task["is_simulated"] and not task["snapshot"]["is_simulated"]
        assert task["snapshot"]["reference"]["transcript"] == transcript
        assert task["snapshot"]["adapter"]["model_fingerprint"] == config["model_fingerprint"]
        assert str(tmp_path) not in json.dumps(task)
        assert (
            client.put(base, json={**body, "expected_revision": 1, "text": "new words"}).status_code
            == 200
        )
        assert (
            client.get(f"/api/projects/{project}/tasks/{task['id']}").json()["snapshot"]
            == task["snapshot"]
        )
        real_input["request_id"] = str(uuid4())
        assert client.post(f"/api/projects/{project}/tasks", json=real_input).status_code == 409
        real_input["expected_transcript_revision"] = 2
        real_input["expected_config_fingerprint"] = "0" * 64
        assert client.post(f"/api/projects/{project}/tasks", json=real_input).status_code == 409
        real_input["expected_config_fingerprint"] = config["config_fingerprint"]
        real_input["parameters"] = {"speed": 2}
        assert client.post(f"/api/projects/{project}/tasks", json=real_input).status_code == 422


@pytest.mark.parametrize(
    "case",
    [
        "missing-version",
        "missing-config",
        "missing-transcript",
        "empty-transcript",
        "long-reference",
    ],
)
def test_real_input_guards(tmp_path: Path, case: str) -> None:
    location, config = fixture_model(tmp_path / "model-fixture")
    directory = tmp_path / "data"
    database, project, payload = ready_project(directory)
    task, _ = enqueue(database, directory, project, TaskInput(**payload), GenerationRegistry())
    real = {
        **payload,
        "request_id": str(uuid4()),
        "adapter_id": "cosyvoice3-local",
        "expected_adapter_version": "1",
        "expected_config_fingerprint": config["config_fingerprint"],
        "expected_transcript_revision": 1,
    }
    with TestClient(
        create_app(directory, worker_enabled=False, model_config_path=location)
    ) as client:
        base = transcript_base(project, task)
        original = client.get(base).json()
        text = "" if case == "empty-transcript" else "words"
        assert (
            client.put(
                base,
                json={
                    "expected_revision": 0,
                    "expected_clip_sha256": original["clip_sha256"],
                    "text": text,
                },
            ).status_code
            == 200
        )
        if case == "missing-version":
            real.pop("expected_adapter_version")
        elif case == "missing-config":
            real.pop("expected_config_fingerprint")
        elif case == "missing-transcript":
            real.pop("expected_transcript_revision")
        elif case == "long-reference":
            from backend.tests.test_references import wav_bytes

            reference = task["snapshot"]["reference"]
            path = (
                directory
                / "references"
                / reference["id"]
                / "clips"
                / f"{reference['clip']['id']}.wav"
            )
            path.write_bytes(wav_bytes(31000))
            with connect(database) as connection:
                connection.execute(
                    "UPDATE reference_clips SET end_ms=31000 WHERE id=?", (reference["clip"]["id"],)
                )
        response = client.post(f"/api/projects/{project}/tasks", json=real)
        assert response.status_code == (
            422 if case in ("empty-transcript", "long-reference") else 409
        )
        assert len(client.get(f"/api/projects/{project}/tasks").json()["tasks"]) == 1


def test_m04_schema_snapshot_and_request_replay_upgrade(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    payload.update(mode="full", paragraph_id=None)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    old_snapshot = task["snapshot"]
    old_snapshot.pop("output_contract", None)
    old_snapshot["adapter"] = M04_CAPABILITY
    old_fingerprint = canonical(
        {
            "operation": "submit",
            **{key: value for key, value in payload.items() if key != "request_id"},
        }
    )
    with connect(database) as connection:
        # Construct the actual pre-005 schema and literal M04 JSON/fingerprint, then upgrade it.
        connection.execute("DROP TABLE reference_transcripts")
        connection.execute("DROP TABLE paragraph_audio_selections")
        connection.execute("DROP TABLE project_deletions")
        connection.execute("DROP TABLE deleted_project_requests")
        connection.execute("DROP TABLE project_archives")
        connection.execute("DROP TABLE project_create_requests")
        connection.execute("DROP TABLE abandoned_project_requests")
        connection.execute("DROP TABLE task_deletions")
        connection.execute("DROP TABLE asset_deletions")
        connection.execute("DELETE FROM schema_migrations WHERE version>=5")
        connection.execute(
            "UPDATE generation_tasks SET snapshot_json=? WHERE id=?",
            (canonical(old_snapshot), task["id"]),
        )
        connection.execute("UPDATE generation_requests SET fingerprint=?", (old_fingerprint,))
    migrate(database)
    repeated, created = enqueue(
        database, tmp_path, project, TaskInput(**payload), GenerationRegistry()
    )
    assert not created and repeated["id"] == task["id"]
    assert repeated["snapshot"] == old_snapshot
    with WorkerSession(tmp_path) as worker:
        assert worker.run_once()
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        finished = client.get(f"/api/projects/{project}/tasks/{task['id']}").json()
        assert finished["state"] == "succeeded" and finished["is_simulated"]
        assert finished["snapshot"]["adapter"] == M04_CAPABILITY
        assert finished["result"]["input_sha256"] == finished["snapshot_sha256"]
        assert client.get(f"/api/projects/{project}/tasks/{task['id']}/audio").status_code == 200
    assert (
        file_digest(next((tmp_path / "generation-results").glob("*/audio.wav")))
        == finished["result"]["sha256"]
    )
