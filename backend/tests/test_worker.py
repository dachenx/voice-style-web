import io
import sqlite3
import subprocess
import sys
import threading
import time
import wave
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.generation import GenerationRegistry, SimulatedToneAdapter, TaskInput
from backend.app.main import create_app
from backend.app.storage import connect
from backend.app.tasks import enqueue, file_digest, get_task
from backend.app.worker import WorkerSession
from backend.tests.test_api import server
from backend.tests.test_references import wav_bytes
from backend.tests.test_task_store import ready_project


class BlockingAdapter(SimulatedToneAdapter):
    def generate(self, snapshot: dict[str, Any], destination: Path, progress: Any) -> None:
        destination.write_bytes(b"partial")
        (destination.parents[2] / "worker-entered").write_text("entered")
        threading.Event().wait(30)


def task_state(database: Path, project: str, task_id: str) -> dict[str, Any]:
    with connect(database) as connection:
        return get_task(connection, project, task_id)


def test_worker_generates_real_wav_and_correlates_snapshot(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    payload.update(mode="full", paragraph_id=None)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    with WorkerSession(tmp_path) as worker:
        assert worker.run_once()
        assert not worker.run_once()
    finished = task_state(database, project, task["id"])
    assert finished["state"] == "succeeded" and finished["phase"] == "succeeded"
    assert finished["completed_paragraphs"] == finished["total_paragraphs"] == 2
    assert finished["state_version"] > task["state_version"]
    result = finished["result"]
    assert result["input_sha256"] == finished["snapshot_sha256"] == task["snapshot_sha256"]
    assert result["elapsed_ms"] >= 0
    path = tmp_path / "generation-results" / task["id"] / "audio.wav"
    assert result["sha256"] == file_digest(path)
    with wave.open(str(path), "rb") as audio:
        assert audio.getnframes() == result["frame_count"] == 12000
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (24000, 1, 2)
    assert result["duration_ms"] == 500
    assert not list((tmp_path / "generation-tmp").iterdir())


@pytest.mark.parametrize(
    "failure",
    ["exception", "bad-wav", "missing-progress", "bad-progress", "storage", "reference", "stop"],
)
def test_worker_failures_never_publish_success(tmp_path: Path, failure: str) -> None:
    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    registry = GenerationRegistry()
    worker = WorkerSession(tmp_path, registry)

    def generate(snapshot: dict[str, Any], destination: Path, progress: Any) -> None:
        if failure == "exception":
            raise RuntimeError("private credential path")
        if failure == "stop":
            worker.stop.set()
            progress(1)
        elif failure == "storage":
            raise OSError("private disk path")
        elif failure == "bad-progress":
            progress(True)
        else:
            destination.write_bytes(b"invalid" if failure == "bad-wav" else wav_bytes())
            if failure != "missing-progress":
                progress(1)

    if failure == "reference":
        path = next((tmp_path / "references").glob("*/clips/*.wav"))
        changed = bytearray(path.read_bytes())
        changed[-2:] = b"\x02\x00"
        path.write_bytes(changed)
    with (
        worker,
        patch.object(registry.adapters["simulated-tone"], "generate", side_effect=generate),
    ):
        assert worker.run_once()
    failed = task_state(database, project, task["id"])
    assert failed["state"] == "failed" and failed["result"] is None
    assert failed["snapshot"] == task["snapshot"]
    assert "private" not in failed["error"]["message"]
    assert not (tmp_path / "generation-results" / task["id"]).exists()
    assert not list((tmp_path / "generation-tmp").glob("*"))


def test_result_commit_failure_rolls_back_and_cleans_output(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    with connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER fail_success BEFORE UPDATE ON generation_tasks "
            "WHEN NEW.state='succeeded' BEGIN SELECT RAISE(ABORT,'fixture'); END"
        )
    with WorkerSession(tmp_path) as worker:
        assert worker.run_once()
    failed = task_state(database, project, task["id"])
    assert failed["state"] == "failed" and failed["result"] is None
    assert failed["error"]["code"] == "storage_unavailable"
    assert not (tmp_path / "generation-results" / task["id"]).exists()


def test_adapter_cannot_mutate_recorded_snapshot_or_result_input_digest(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    registry = GenerationRegistry()

    def mutate(snapshot: dict[str, Any], destination: Path, progress: Any) -> None:
        SimulatedToneAdapter().generate(snapshot, destination, progress)
        snapshot["confirmation"]["register"] = "adapter mutation"
        snapshot["reference"]["filename"] = "mutated.wav"

    with (
        WorkerSession(tmp_path, registry) as worker,
        patch.object(registry.adapters["simulated-tone"], "generate", side_effect=mutate),
    ):
        assert worker.run_once()
    finished = task_state(database, project, task["id"])
    assert finished["state"] == "succeeded"
    assert finished["snapshot"] == task["snapshot"]
    assert finished["result"]["input_sha256"] == task["snapshot_sha256"]


def test_database_failure_cannot_mark_success_and_recovers_after_restart(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    registry = GenerationRegistry()

    def unavailable(snapshot: dict[str, Any], destination: Path, progress: Any) -> None:
        destination.write_bytes(wav_bytes())
        with connect(database) as connection:
            connection.execute(
                "CREATE TRIGGER all_task_writes_fail BEFORE UPDATE ON generation_tasks "
                "BEGIN SELECT RAISE(ABORT,'fixture db unavailable'); END"
            )
        progress(1)

    with (
        WorkerSession(tmp_path, registry) as worker,
        patch.object(registry.adapters["simulated-tone"], "generate", side_effect=unavailable),
        pytest.raises(sqlite3.Error),
    ):
        worker.run_once()
    unfinished = task_state(database, project, task["id"])
    assert unfinished["state"] == "running" and unfinished["result"] is None
    assert not (tmp_path / "generation-results" / task["id"]).exists()
    assert not list((tmp_path / "generation-tmp").glob("*"))
    with connect(database) as connection:
        connection.execute("DROP TRIGGER all_task_writes_fail")
    with WorkerSession(tmp_path):
        pass
    assert task_state(database, project, task["id"])["error"]["code"] == "worker_interrupted"


def test_failed_task_progress_is_actual_completed_count(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    payload.update(mode="full", paragraph_id=None)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    registry = GenerationRegistry()

    def partial(snapshot: dict[str, Any], destination: Path, progress: Any) -> None:
        destination.write_bytes(wav_bytes())
        progress(1)
        assert task_state(database, project, task["id"])["completed_paragraphs"] == 1
        raise RuntimeError("second paragraph failed")

    with (
        WorkerSession(tmp_path, registry) as worker,
        patch.object(registry.adapters["simulated-tone"], "generate", side_effect=partial),
    ):
        worker.run_once()
    failed = task_state(database, project, task["id"])
    assert failed["state"] == "failed"
    assert failed["completed_paragraphs"] == 1 and failed["total_paragraphs"] == 2


def test_failed_task_http_retry_executes_original_snapshot_to_success(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    payload.update(mode="full", paragraph_id=None)
    base = f"/api/projects/{project}/tasks"
    registry = GenerationRegistry()
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        created = client.post(base, json=payload)
        assert created.status_code == 201
        original = created.json()
        with (
            WorkerSession(tmp_path, registry) as worker,
            patch.object(
                registry.adapters["simulated-tone"],
                "generate",
                side_effect=RuntimeError("fixture generation failure"),
            ),
        ):
            assert worker.run_once()
        failed = client.get(f"{base}/{original['id']}").json()
        assert failed["state"] == "failed"
        assert failed["error"]["code"] == "generation_failed"
        assert failed["snapshot"] == original["snapshot"]

        # Change and confirm the current text, and select a different real reference clip.
        content = {
            key: original["snapshot"]["confirmation"][key]
            for key in (
                "source_language",
                "target_language",
                "register",
                "terms",
            )
        }
        content["paragraphs"] = [
            {
                "id": str(uuid4()),
                "original_text": "new original",
                "draft_text": "new draft",
                "pronunciation_note": "new note",
            }
        ]
        text_base = f"/api/projects/{project}/text"
        assert client.put(text_base, json={**content, "expected_revision": 1}).status_code == 200
        new_confirmation = client.post(
            f"{text_base}/confirm",
            json={
                "expected_revision": 2,
                "acknowledged": True,
            },
        ).json()["latest_confirmation"]
        reference_base = f"/api/projects/{project}/references"
        uploaded = client.post(
            reference_base + "?filename=new.wav",
            content=wav_bytes(),
            headers={"content-type": "application/octet-stream"},
        )
        assert uploaded.status_code == 201, uploaded.text
        selected = client.post(
            f"{reference_base}/{uploaded.json()['id']}/clips", json={"start_ms": 100, "end_ms": 400}
        )
        assert selected.status_code == 201
        assert new_confirmation["id"] != original["snapshot"]["confirmation"]["id"]
        assert selected.json()["id"] != original["snapshot"]["reference"]["clip"]["id"]

        request = {"request_id": str(uuid4())}
        response = client.post(f"{base}/{original['id']}/retry", json=request)
        assert response.status_code == 201, response.text
        retry = response.json()
        assert retry["id"] != original["id"] and retry["retry_of"] == original["id"]
        assert retry["snapshot"] == original["snapshot"]
        repeated = client.post(f"{base}/{original['id']}/retry", json=request)
        assert repeated.status_code == 200 and repeated.json()["id"] == retry["id"]
        with WorkerSession(tmp_path) as worker:
            assert worker.run_once()
        finished = client.get(f"{base}/{retry['id']}").json()
        assert finished["state"] == "succeeded"
        assert finished["completed_paragraphs"] == finished["total_paragraphs"] == 2
        assert finished["snapshot"] == original["snapshot"]
        assert finished["result"]["input_sha256"] == original["snapshot_sha256"]
        expected = tmp_path / "expected-simulated.wav"
        SimulatedToneAdapter().generate(original["snapshot"], expected, lambda completed: None)
        audio = client.get(f"{base}/{retry['id']}/audio")
        assert audio.status_code == 200
        assert audio.content == expected.read_bytes()
        assert finished["result"]["sha256"] == file_digest(expected)
        with wave.open(io.BytesIO(audio.content), "rb") as decoded:
            assert decoded.getnframes() == finished["result"]["frame_count"] == 12000
        assert client.get(f"{base}/{original['id']}").json() == failed
        assert len(client.get(base).json()["tasks"]) == 2
        assert task_state(database, project, retry["id"])["snapshot"] == original["snapshot"]


def test_task_api_isolation_cancel_retry_and_output(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    base = f"/api/projects/{project}/tasks"
    with TestClient(create_app(tmp_path, worker_enabled=False)) as client:
        assert client.get("/api/generation-adapters").json()["adapters"][0]["is_simulated"]
        created = client.post(base, json=payload)
        assert created.status_code == 201, created.text
        task = created.json()
        assert client.post(base, json=payload).status_code == 200
        listing = client.get(base).json()
        assert listing["tasks"][0]["id"] == task["id"]
        assert "snapshot" not in listing["tasks"][0]
        assert listing["worker"]["state"] == "stopped"
        other = client.post("/api/projects", json={"name": "other"}).json()["id"]
        wrong = f"/api/projects/{other}/tasks/{task['id']}"
        for suffix in ("", "/audio", "/download"):
            assert client.get(wrong + suffix).status_code == 404
        assert client.post(wrong + "/cancel").status_code == 404
        assert client.post(wrong + "/retry", json={"request_id": str(uuid4())}).status_code == 404
        assert client.get(f"{base}/{task['id']}/audio").status_code == 409
        canceled = client.post(f"{base}/{task['id']}/cancel").json()
        assert canceled["state"] == "canceled"
        assert client.post(f"{base}/{task['id']}/cancel").json() == canceled
        assert (
            client.post(f"{base}/{task['id']}/retry", json={"request_id": str(uuid4())}).status_code
            == 409
        )
        payload["request_id"] = str(uuid4())
        next_task = client.post(base, json=payload).json()
        with WorkerSession(tmp_path) as worker:
            assert worker.run_once()
        location = f"{base}/{next_task['id']}"
        finished = client.get(location).json()
        assert finished["state"] == "succeeded"
        audio = client.get(location + "/audio")
        assert audio.status_code == 200
        with wave.open(io.BytesIO(audio.content), "rb") as decoded:
            assert decoded.getnframes() == finished["result"]["frame_count"]
        assert (
            client.get(location + "/audio", headers={"Range": "bytes=0-43"}).content
            == audio.content[:44]
        )
        download = client.get(location + "/download")
        assert download.content == audio.content
        assert "attachment" in download.headers["content-disposition"]
        assert "simulated-" in download.headers["content-disposition"]
        assert client.post(location + "/cancel").status_code == 409
        (tmp_path / "generation-results" / next_task["id"] / "audio.wav").write_bytes(b"bad")
        assert client.get(location + "/audio").status_code == 503


def test_cancel_and_claim_real_transaction_race(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    base = f"/api/projects/{project}/tasks"
    barrier = threading.Barrier(3)
    with (
        TestClient(create_app(tmp_path, worker_enabled=False)) as client,
        WorkerSession(tmp_path) as worker,
    ):

        def claim() -> dict[str, Any] | None:
            barrier.wait(timeout=5)
            return worker.claim()

        def cancel() -> httpx.Response:
            barrier.wait(timeout=5)
            return client.post(f"{base}/{task['id']}/cancel")

        with ThreadPoolExecutor(max_workers=2) as executor:
            claim_future = executor.submit(claim)
            cancel_future = executor.submit(cancel)
            barrier.wait(timeout=5)
            claimed = claim_future.result(timeout=10)
            canceled = cancel_future.result(timeout=10)
        final = task_state(database, project, task["id"])
        if claimed is None:
            assert canceled.status_code == 200
            assert final["state"] == "canceled"
            # Also prove running cancellation persists a stop request.
            payload["request_id"] = str(uuid4())
            running, _ = enqueue(
                database, tmp_path, project, TaskInput(**payload), GenerationRegistry()
            )
            claimed = worker.claim()
            assert claimed is not None and claimed["id"] == running["id"]
        else:
            assert canceled.status_code == 200
            assert final["state"] == "running"
            assert final["phase"] == "cancel_requested"
        assert client.post(f"{base}/{claimed['id']}/cancel").status_code == 200
        assert task_state(database, project, claimed["id"])["state"] == "running"


@contextmanager
def blocked_worker(directory: Path) -> Iterator[subprocess.Popen[bytes]]:
    code = (
        "from pathlib import Path; import sys; "
        "from backend.app.worker import WorkerSession; "
        "from backend.app.generation import GenerationRegistry; "
        "from backend.tests.test_worker import BlockingAdapter; "
        "worker=WorkerSession(Path(sys.argv[1]),GenerationRegistry([BlockingAdapter()])); "
        "worker.__enter__(); worker.run_once(); worker.__exit__(None,None,None)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(directory)],
        cwd=Path(__file__).parents[2],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (directory / "worker-entered").exists():
                break
            assert process.poll() is None
            time.sleep(0.02)
        else:
            raise AssertionError("blocking worker did not start")
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_live_worker_mutex_and_kill_recovery_preserve_queue(tmp_path: Path) -> None:
    database, project, payload = ready_project(tmp_path)
    running, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    payload["request_id"] = str(uuid4())
    queued, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    with blocked_worker(tmp_path) as old:
        assert task_state(database, project, running["id"])["state"] == "running"
        competing = subprocess.run(
            [sys.executable, "-m", "backend.app.worker", "--data-dir", str(tmp_path)],
            cwd=Path(__file__).parents[2],
            capture_output=True,
            timeout=10,
        )
        assert competing.returncode != 0
        assert task_state(database, project, running["id"])["state"] == "running"
        # A newly-started API may not recover/delete the still-live worker's task/output.
        with TestClient(create_app(tmp_path)) as client:
            time.sleep(0.2)
            assert task_state(database, project, running["id"])["state"] == "running"
            assert (tmp_path / "generation-tmp" / running["id"] / "audio.wav").exists()
            assert (
                client.get(f"/api/projects/{project}/tasks/{queued['id']}").json()["state"]
                == "queued"
            )
        assert old.poll() is None
        old.kill()
        old.wait(timeout=5)
    with WorkerSession(tmp_path) as recovered:
        interrupted = task_state(database, project, running["id"])
        assert interrupted["state"] == "failed"
        assert interrupted["error"]["code"] == "worker_interrupted"
        assert interrupted["snapshot"] == running["snapshot"]
        assert not (tmp_path / "generation-tmp" / running["id"]).exists()
        assert task_state(database, project, queued["id"])["state"] == "queued"
        assert recovered.run_once()
    assert task_state(database, project, queued["id"])["state"] == "succeeded"


def test_parent_pipe_close_exits_worker(tmp_path: Path) -> None:
    ready_project(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "backend.app.worker", "--data-dir", str(tmp_path), "--watch-parent"],
        cwd=Path(__file__).parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_sigterm_exits_worker_with_parent_pipe_still_open(tmp_path: Path) -> None:
    ready_project(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "backend.app.worker", "--data-dir", str(tmp_path), "--watch-parent"],
        cwd=Path(__file__).parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            with connect(tmp_path / "projects.sqlite3") as connection:
                row = connection.execute("SELECT state FROM generation_worker_health").fetchone()
            if row is not None and row["state"] == "idle":
                break
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert process.stdin is not None and not process.stdin.closed
        process.terminate()
        assert process.wait(timeout=5) == 0
        assert process.stderr is not None
        assert b"Fatal Python error" not in process.stderr.read()
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stderr is not None:
            process.stderr.close()


def test_real_api_worker_restart_keeps_result(tmp_path: Path) -> None:
    _, project, payload = ready_project(tmp_path)
    with server(tmp_path) as url:
        base = f"{url}/api/projects/{project}/tasks"
        response = httpx.post(base, json=payload)
        assert response.status_code == 201, response.text
        task = response.json()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            finished = httpx.get(f"{base}/{task['id']}").json()
            if finished["state"] == "succeeded":
                break
            time.sleep(0.05)
        else:
            raise AssertionError("real worker did not finish")
        original = httpx.get(f"{base}/{task['id']}/audio").content
    with server(tmp_path) as url:
        base = f"{url}/api/projects/{project}/tasks"
        assert httpx.get(f"{base}/{task['id']}").json() == finished
        assert httpx.get(f"{base}/{task['id']}/audio").content == original
    # Service shutdown releases both locks; no worker is left owning this directory.
    with WorkerSession(tmp_path):
        pass


@pytest.mark.parametrize("moment", ["progress", "publish"])
def test_running_cancel_cleans_before_terminal_and_queue_continues(tmp_path, moment):
    database, project, payload = ready_project(tmp_path)
    registry = GenerationRegistry()
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), registry)
    payload["request_id"] = str(uuid4())
    next_task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), registry)
    base = f"/api/projects/{project}/tasks/{task['id']}"
    with (
        TestClient(create_app(tmp_path, worker_enabled=False)) as client,
        WorkerSession(tmp_path, registry) as worker,
    ):
        original = registry.adapters["simulated-tone"].generate

        def generate(snapshot, destination, progress):
            original(snapshot, destination, progress)
            response = client.post(base + "/cancel")
            assert response.status_code == 200
            assert response.json()["state"] == "running"
            assert response.json()["phase"] == "cancel_requested"
            assert client.request("DELETE", base, json={"acknowledged": True}).status_code == 409
            if moment == "progress":
                progress(1)

        with patch.object(registry.adapters["simulated-tone"], "generate", side_effect=generate):
            assert worker.run_once()
        stopped = client.get(base).json()
        assert stopped["state"] == "canceled" and stopped["result"] is None
        assert not (tmp_path / "generation-results" / task["id"]).exists()
        assert not (tmp_path / "generation-tmp" / task["id"]).exists()
        assert worker.run_once()
        assert task_state(database, project, next_task["id"])["state"] == "succeeded"


def test_restart_finishes_persisted_stop_request(tmp_path):
    database, project, payload = ready_project(tmp_path)
    task, _ = enqueue(database, tmp_path, project, TaskInput(**payload), GenerationRegistry())
    with WorkerSession(tmp_path) as worker:
        assert worker.claim()
        with connect(database) as connection:
            connection.execute(
                "UPDATE generation_tasks SET phase='cancel_requested' WHERE id=?", (task["id"],)
            )
    with WorkerSession(tmp_path):
        assert task_state(database, project, task["id"])["state"] == "canceled"
