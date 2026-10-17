import json
import wave
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.generation import GenerationRegistry, SimulatedToneAdapter, TaskInput
from backend.app.main import create_app
from backend.app.segment_outputs import write_manifest
from backend.app.storage import connect
from backend.app.tasks import enqueue
from backend.app.worker import WorkerSession
from backend.tests.test_task_store import ready_project
from backend.tests.test_versions import choose, edit, history
from backend.tests.test_worker import task_state


def full_task(directory: Path) -> tuple[Any, ...]:
    database, project, payload = ready_project(directory)
    payload.update(mode="full", paragraph_id=None)
    task, _ = enqueue(database, directory, project, TaskInput(**payload), GenerationRegistry())
    with WorkerSession(directory) as worker:
        assert worker.run_once()
    return database, project, payload, task_state(database, project, task["id"])


def test_full_segments_are_real_pcm_versions_and_partial_generation_preserves_choice(
    tmp_path: Path,
) -> None:
    database, project, payload, task = full_task(tmp_path)
    assert task["state"] == "succeeded"
    segments = task["result"]["segments"]
    assert [s["paragraph_id"] for s in segments] == task["snapshot"]["paragraph_ids"]
    directory = tmp_path / "generation-results" / task["id"]
    pcm = b""
    for segment in segments:
        with wave.open(str(directory / segment["file"])) as audio:
            assert audio.getnframes() == segment["frame_count"]
            pcm += audio.readframes(audio.getnframes())
    with wave.open(str(directory / "audio.wav")) as audio:
        assert audio.readframes(audio.getnframes()) == pcm
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        catalog = history(client, project)
        assert choose(client, project, catalog).status_code == 200
        selected = catalog["paragraphs"][0]["versions"][0]["id"]
        edit(client, project, "text")
        catalog = history(client, project)
        assert catalog["paragraphs"][0]["versions"][0]["comparison"] == "stale"
        assert catalog["paragraphs"][1]["versions"][0]["comparison"] == "matching"
        doc = client.get(f"/api/projects/{project}/text").json()
        confirmed = client.post(
            f"/api/projects/{project}/text/confirm",
            json={"expected_revision": doc["revision"], "acknowledged": True},
        )
        assert confirmed.status_code == 200
        payload.update(
            request_id=str(uuid4()),
            mode="preview",
            paragraph_id=catalog["paragraphs"][0]["id"],
            expected_revision=doc["revision"],
            expected_confirmation_id=confirmed.json()["latest_confirmation"]["id"],
        )
        child = client.post(f"/api/projects/{project}/tasks", json=payload)
        assert child.status_code == 201, child.text
        with WorkerSession(tmp_path) as worker:
            assert worker.run_once()
        current = history(client, project)
        first, second = current["paragraphs"]
        assert len(first["versions"]) == 2 and len(second["versions"]) == 1
        assert first["selection"]["version_id"] == selected
        assert first["versions"][0]["comparison"] == "matching"
        assert first["versions"][1]["comparison"] == "stale"
        assert first["versions"][0]["id"] != selected
        assert task_state(database, project, task["id"]) == task


@pytest.mark.parametrize("damage", ["missing", "order", "extra", "path", "digest", "pcm", "commit"])
def test_invalid_segments_never_publish_or_replace_old_choice(tmp_path: Path, damage: str) -> None:
    database, project, payload, original = full_task(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        catalog = history(client, project)
        assert choose(client, project, catalog).status_code == 200
        before = history(client, project)
        payload["request_id"] = str(uuid4())
        task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
        registry = GenerationRegistry()

        def broken(snapshot: Any, destination: Path, progress: Any) -> None:
            SimulatedToneAdapter().generate(snapshot, destination, progress)
            path = destination.parent / "segments.json"
            data = json.loads(path.read_text())
            if damage == "missing":
                (destination.parent / data["segments"][0]["file"]).unlink()
            elif damage == "order":
                data["segments"].reverse()
            elif damage == "extra":
                data["segments"].append(data["segments"][0])
            elif damage == "path":
                data["segments"][0]["file"] = "../audio.wav"
            elif damage == "digest":
                data["segments"][0]["sha256"] = "0" * 64
            elif damage == "pcm":
                segment = destination.parent / data["segments"][0]["file"]
                raw = bytearray(segment.read_bytes())
                raw[-2:] = b"\x01\x02"
                segment.write_bytes(raw)
                write_manifest(destination.parent, snapshot["paragraph_ids"])
                return
            path.write_text(json.dumps(data))

        if damage == "commit":
            with connect(database) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_new BEFORE UPDATE ON generation_tasks "
                    "WHEN NEW.state='succeeded' BEGIN SELECT RAISE(ABORT,'fixture'); END"
                )
        with patch.object(registry.adapters["simulated-tone"], "generate", side_effect=broken):
            with WorkerSession(tmp_path, registry) as worker:
                assert worker.run_once()
        failed = task_state(database, project, task["id"])
        assert failed["state"] == "failed" and failed["result"] is None
        assert not (tmp_path / "generation-results" / task["id"]).exists()
        assert not list((tmp_path / "generation-tmp").iterdir())
        assert history(client, project) == before
        assert task_state(database, project, original["id"]) == original


@pytest.mark.parametrize("contract", [None, "future-contract"])
def test_legacy_retry_and_unknown_contract_are_not_silently_upgraded(
    tmp_path: Path, contract: str | None
) -> None:
    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    snapshot = task["snapshot"]
    if contract is None:
        snapshot.pop("output_contract")
    else:
        snapshot["output_contract"] = contract
    with connect(database) as connection:
        connection.execute(
            "UPDATE generation_tasks SET snapshot_json=? WHERE id=?",
            (json.dumps(snapshot), task["id"]),
        )
    registry = GenerationRegistry()
    with patch.object(
        registry.adapters["simulated-tone"], "generate", side_effect=RuntimeError("fixture")
    ):
        with WorkerSession(tmp_path, registry) as worker:
            assert worker.run_once()
    failed = task_state(database, project, task["id"])
    assert failed["state"] == "failed"
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        response = client.post(
            f"/api/projects/{project}/tasks/{task['id']}/retry", json={"request_id": str(uuid4())}
        )
        assert response.status_code == 201
        retried = response.json()
        assert retried["snapshot"] == snapshot
        with WorkerSession(tmp_path) as worker:
            assert worker.run_once()
        final = task_state(database, project, retried["id"])
        if contract is None:
            assert final["state"] == "succeeded" and "segments" not in final["result"]
            assert history(client, project)["paragraphs"][0]["versions"][0]["legacy"]
        else:
            assert final["error"]["code"] == "output_contract_unsupported"
            assert final["result"] is None
            assert not history(client, project)["paragraphs"][0]["versions"]


def test_kill_after_directory_rename_before_success_commit_preserves_old_choice(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys
    import time

    database, project, payload, original = full_task(tmp_path)
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        catalog = history(client, project)
        assert choose(client, project, catalog).status_code == 200
        before = history(client, project)
        payload["request_id"] = str(uuid4())
        task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
        code = """
import sys, time
from pathlib import Path
from unittest.mock import patch
from backend.app.worker import WorkerSession
directory = Path(sys.argv[1])
rename = Path.rename
def stop_after_rename(source, target):
    result = rename(source, target)
    (directory / 'renamed').write_text('ready')
    time.sleep(60)
    return result
with patch.object(Path, 'rename', stop_after_rename):
    with WorkerSession(directory) as worker:
        worker.run_once()
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)])
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / "renamed").exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.02)
            target = tmp_path / "generation-results" / task["id"]
            assert (target / "segments.json").exists()
            assert task_state(database, project, task["id"])["state"] == "running"
            assert history(client, project) == before
            process.kill()
            process.wait(timeout=5)
            with WorkerSession(tmp_path):
                assert not target.exists()
            assert (
                task_state(database, project, task["id"])["error"]["code"] == "worker_interrupted"
            )
            assert task_state(database, project, original["id"]) == original
            assert history(client, project) == before
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
