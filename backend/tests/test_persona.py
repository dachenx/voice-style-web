"""Persona integration with synthetic assets; no voice-quality claim."""

import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.generation import GenerationRegistry, TaskInput
from backend.app.local_qwen import LocalQwenAdapter
from backend.app.main import create_app
from backend.app.model_config import ModelConfigError, digest_json
from backend.app.persona_config import PERSONA_ID, identity, install
from backend.app.storage import connect
from backend.app.tasks import enqueue
from backend.app.worker import WorkerSession
from backend.tests.test_project_deletion import remove
from backend.tests.test_references import wav_bytes
from backend.tests.test_task_store import ready_project
from backend.tests.test_worker import task_state


def fixture_persona(directory):
    directory.mkdir(parents=True)
    ref = directory / "reference.wav"
    ref.write_bytes(wav_bytes())
    artifact = identity(ref)
    value = {
        "schema_version": 1,
        "persona_id": PERSONA_ID,
        "model_id": "test-qwen",
        "model_revision": "fixture",
        "model_directory": str(directory),
        "adapter_path": str(ref),
        "reference_path": str(ref),
        "reference_text": "synthetic",
        "reference_sha256": artifact["sha256"],
        "reference_duration_ms": 1000,
        "python_executable": sys.executable,
        "files": [artifact],
        "model_fingerprint": "a" * 64,
        "environment": {"python_version": "fixture", "packages": {"fixture": "1"}},
        "sampling": {
            "temperature": 0.9,
            "top_p": 1.0,
            "top_k": 50,
            "repetition_penalty": 1.05,
            "max_tokens": 512,
        },
        "seed": 42,
        "actual_icl_repetition_penalty": 1.5,
        "limits": {
            "max_text_characters": 80,
            "task_timeout_seconds": 10,
            "paragraph_timeout_seconds": 5,
            "model_load_timeout_seconds": 5,
        },
    }
    value["config_fingerprint"] = digest_json(value)
    location = directory / "config.json"
    install(value, location)
    return location, value


def setup(tmp_path, monkeypatch):
    location, config = fixture_persona(tmp_path / "shared-persona")
    directory = tmp_path / "data"
    database, project, payload = ready_project(directory)
    with connect(database) as db:
        db.execute("DELETE FROM project_reference_selection")
        db.execute("DELETE FROM reference_clips")
        db.execute("DELETE FROM audio_references")
    payload.update(
        adapter_id=PERSONA_ID,
        expected_clip_id=None,
        mode="full",
        paragraph_id=None,
        expected_adapter_version="0.11",
        expected_config_fingerprint=config["config_fingerprint"],
    )
    adapter = LocalQwenAdapter(location, runtime_directory=directory / "model-health")
    original = LocalQwenAdapter.frozen

    def frozen(self, fingerprint):
        result = original(self, fingerprint)
        result.bridge_command = (
            sys.executable,
            str(Path(__file__).with_name("bridge_runner.py")),
            "--control",
            str(tmp_path / "behavior"),
        )
        result.bridge_timeout = 2
        return result

    monkeypatch.setattr(LocalQwenAdapter, "frozen", frozen)
    return directory, database, project, payload, adapter, config


def test_no_project_reference_snapshot_worker_export_and_delete(tmp_path, monkeypatch):
    directory, database, project, payload, adapter, config = setup(tmp_path, monkeypatch)
    registry = GenerationRegistry([adapter])
    task, created = enqueue(database, directory, project, TaskInput(**payload), registry)
    assert created and task["snapshot"]["reference_source"] == "persona"
    assert task["snapshot"]["generation_settings"]["actual_icl_repetition_penalty"] == 1.5
    same, created = enqueue(database, directory, project, TaskInput(**payload), registry)
    assert not created and same["id"] == task["id"]
    with WorkerSession(directory, registry) as worker:
        assert worker.run_once()
    finished = task_state(database, project, task["id"])
    assert finished["state"] == "succeeded", finished
    assert len(finished["result"]["segments"]) == 2
    with TestClient(
        create_app(directory, generation_adapters=[adapter], worker_enabled=False)
    ) as c:
        base = f"/api/projects/{project}"

        def catalogue():
            return c.get(base + "/paragraph-versions", params={"adapter_id": PERSONA_ID}).json()

        current = catalogue()
        assert not current["readiness"]
        for paragraph in current["paragraphs"]:
            response = c.put(
                base + f"/paragraphs/{paragraph['id']}/selection",
                json={
                    "adapter_id": PERSONA_ID,
                    "expected_revision": 0,
                    "version_id": paragraph["versions"][0]["id"],
                    "comparison_token": catalogue()["comparison_token"],
                },
            )
            assert response.status_code == 200, response.text
        current = catalogue()
        output = c.post(
            base + "/exports",
            json={
                "request_id": str(uuid4()),
                "adapter_id": PERSONA_ID,
                "comparison_token": current["comparison_token"],
                "selections": {p["id"]: p["selection"]["revision"] for p in current["paragraphs"]},
                "pause_ms": 0,
            },
        )
        assert output.status_code == 200, output.text
        assert not output.json()["is_simulated"]
        assert c.get(base + f"/exports/{output.json()['id']}/audio").content[:4] == b"RIFF"
        assert remove(c, project, "task fixture").status_code == 200
        assert Path(config["reference_path"]).is_file()
        adapter.require_configuration()


@pytest.mark.parametrize("failure", ["fail", "block", "asset-change", "missing-archive"])
def test_persona_failures_do_not_publish_or_fallback(tmp_path, monkeypatch, failure):
    directory, database, project, payload, adapter, config = setup(tmp_path, monkeypatch)
    registry = GenerationRegistry([adapter])
    task, _ = enqueue(database, directory, project, TaskInput(**payload), registry)
    if failure == "asset-change":
        Path(config["reference_path"]).write_bytes(b"changed")
    elif failure == "missing-archive":
        (
            adapter.config_path.parent
            / "persona-configs"
            / (config["config_fingerprint"] + ".json")
        ).unlink()
    else:
        (tmp_path / "behavior").write_text(failure)
    with WorkerSession(directory, registry) as worker:
        assert worker.run_once()
    finished = task_state(database, project, task["id"])
    assert finished["state"] == "failed" and finished["result"] is None
    assert finished["snapshot"] == task["snapshot"]
    assert not (directory / "generation-results" / task["id"]).exists()


def test_persona_rejects_wrong_version_project_reference_and_long_text(tmp_path, monkeypatch):
    directory, database, project, payload, adapter, _ = setup(tmp_path, monkeypatch)
    with TestClient(
        create_app(directory, generation_adapters=[adapter], worker_enabled=False)
    ) as c:
        base = f"/api/projects/{project}"
        assert (
            c.post(base + "/tasks", json={**payload, "expected_clip_id": str(uuid4())}).status_code
            == 422
        )
        assert (
            c.post(
                base + "/tasks", json={**payload, "expected_config_fingerprint": "b" * 64}
            ).status_code
            == 409
        )
        with connect(database) as db:
            row = db.execute("SELECT snapshot_json FROM text_confirmations").fetchone()
            content = json.loads(row[0])
            content["paragraphs"][0]["draft_text"] = "长" * 81
            db.execute("UPDATE text_confirmations SET snapshot_json=?", (json.dumps(content),))
        assert c.post(base + "/tasks", json=payload).status_code == 422
        assert c.get(base + "/tasks").json()["tasks"] == []


def test_same_version_cannot_be_rebound(tmp_path):
    location, config = fixture_persona(tmp_path / "persona")
    changed = {**config, "reference_text": "different"}
    changed.pop("config_fingerprint")
    changed["config_fingerprint"] = digest_json(changed)
    with pytest.raises(ModelConfigError):
        install(changed, location)
    assert json.loads(location.read_text()) == config


def test_missing_pointer_preserves_archived_retry(tmp_path, monkeypatch):
    from backend.app.generation import RetryInput
    from backend.app.tasks import retry_task

    directory, database, project, payload, adapter, config = setup(tmp_path, monkeypatch)
    monkeypatch.setenv("VOICE_PERSONA_CONFIG", str(adapter.config_path))
    registry = GenerationRegistry()
    task, _ = enqueue(database, directory, project, TaskInput(**payload), registry)
    (tmp_path / "behavior").write_text("fail")
    with WorkerSession(directory, registry) as worker:
        assert worker.run_once()
    adapter.config_path.unlink()
    (tmp_path / "behavior").unlink()
    registry = GenerationRegistry()
    assert not registry.health(PERSONA_ID)["execution_available"]
    child, _ = retry_task(database, project, task["id"], RetryInput(request_id=uuid4()))
    assert child["snapshot"] == task["snapshot"]
    with WorkerSession(directory, registry) as worker:
        assert worker.run_once()
    assert task_state(database, project, child["id"])["state"] == "succeeded"


def test_install_reuses_archive_and_failed_first_publish_leaves_no_archive(tmp_path, monkeypatch):
    location, config = fixture_persona(tmp_path / "persona")
    archive = location.parent / "persona-configs" / (config["config_fingerprint"] + ".json")
    before = archive.stat()
    install(config, location)
    assert archive.stat().st_mtime_ns == before.st_mtime_ns
    destination = tmp_path / "other" / "runtime.json"
    original = Path.replace

    def fail_archive(self, target):
        if Path(target).parent.name == "persona-configs":
            raise OSError("injected publish failure")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", fail_archive)
    with pytest.raises(OSError):
        install(config, destination)
    assert not destination.exists()
    assert not (destination.parent / "persona-configs" / archive.name).exists()
    assert json.loads(archive.read_text()) == config


def test_bridge_rejects_non_icl_and_multichannel_shapes():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from scripts.qwen_bridge import BridgeFailure, mono_audio, require_icl

    valid = SimpleNamespace(
        config=SimpleNamespace(tts_model_type="base"),
        speech_tokenizer=SimpleNamespace(has_encoder=True),
    )
    require_icl(valid)
    for model_type, encoder in [("custom_voice", True), ("base", False)]:
        with pytest.raises(BridgeFailure):
            require_icl(
                SimpleNamespace(
                    config=SimpleNamespace(tts_model_type=model_type),
                    speech_tokenizer=SimpleNamespace(has_encoder=encoder),
                )
            )
    for shape in [(100,), (1, 100), (100, 1)]:
        audio = Mock(ndim=len(shape), shape=shape)
        mono_audio(audio)
    for shape in [(), (2, 100), (1, 2, 100)]:
        audio = Mock(ndim=len(shape), shape=shape)
        with pytest.raises(BridgeFailure):
            mono_audio(audio)
        audio.reshape.assert_not_called()
