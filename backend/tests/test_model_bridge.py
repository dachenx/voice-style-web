import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.generation import GenerationRegistry, SimulatedToneAdapter, TaskError
from backend.app.local_model import LocalCosyVoiceAdapter
from backend.app.main import create_app
from backend.app.model_config import digest_json, save_config
from backend.app.storage import connect
from backend.app.worker import WorkerSession
from backend.tests.test_model_inputs import fixture_model
from backend.tests.test_task_store import ready_project
from backend.tests.test_worker import task_state
from scripts.cosyvoice_bridge import BridgeFailure, run, validate_text_tokens

RUNNER = Path(__file__).with_name("bridge_runner.py")


def setup_real(
    tmp_path: Path, timeout: float = 10, load_timeout: float | None = None, *, full: bool = False
) -> tuple[Any, ...]:
    location, config = fixture_model(tmp_path / "model")
    if load_timeout is not None:
        config["limits"] = {**config["limits"], "model_load_timeout_seconds": load_timeout}
        config["config_fingerprint"] = digest_json(
            {key: value for key, value in config.items() if key != "config_fingerprint"}
        )
        save_config(config, location)
    directory = tmp_path / "data"
    database, project, payload = ready_project(directory)
    control = tmp_path / "behavior"
    adapter = LocalCosyVoiceAdapter(
        location,
        runtime_directory=directory / "model-health",
        bridge_command=[sys.executable, str(RUNNER), "--control", str(control)],
        bridge_timeout=timeout,
    )
    adapters = [SimulatedToneAdapter(), adapter]
    registry = GenerationRegistry(adapters)
    app = create_app(directory, generation_adapters=adapters, worker_enabled=False)
    client = TestClient(app)
    client.__enter__()
    reference = client.get(f"/api/projects/{project}/references").json()["references"][0]
    base = f"/api/projects/{project}/references/{reference['id']}/clips"
    base += f"/{payload['expected_clip_id']}/transcript"
    initial = client.get(base).json()
    assert (
        client.put(
            base,
            json={
                "expected_revision": 0,
                "expected_clip_sha256": initial["clip_sha256"],
                "text": "fixture accurate words",
            },
        ).status_code
        == 200
    )
    payload.update(
        adapter_id="cosyvoice3-local",
        expected_transcript_revision=1,
        expected_adapter_version="1",
        expected_config_fingerprint=config["config_fingerprint"],
    )
    if full:
        payload.update(mode="full", paragraph_id=None)
    response = client.post(f"/api/projects/{project}/tasks", json=payload)
    assert response.status_code == 201, response.text
    return (
        client,
        directory,
        database,
        project,
        response.json(),
        registry,
        control,
        config,
        location,
    )


def assert_result(directory: Path, task: dict[str, Any], original: dict[str, Any]) -> None:
    assert task["state"] == "succeeded", task
    assert task["snapshot"] == original["snapshot"]
    assert task["result"]["input_sha256"] == original["snapshot_sha256"]
    assert task["result"]["reference_sha256"] == original["snapshot"]["reference"]["clip_sha256"]
    with wave.open(str(directory / "generation-results" / task["id"] / "audio.wav")) as audio:
        assert audio.getnframes() == task["result"]["frame_count"]
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (24000, 1, 2)
    assert not list((directory / "generation-tmp").iterdir())


def test_subprocess_receives_frozen_inputs_and_publishes_correlated_wav(tmp_path: Path) -> None:
    client, directory, db, project, task, registry, control, config, _ = setup_real(tmp_path)
    try:
        with WorkerSession(directory, registry) as worker:
            assert worker.run_once()
        finished = task_state(db, project, task["id"])
        assert_result(directory, finished, task)
        received = json.loads(control.with_suffix(".request.json").read_text())
        assert received["snapshot"] == task["snapshot"]
        assert received["configuration"] == config
        assert not Path(received["reference_path"]).exists()
        assert not registry.health("cosyvoice3-local")["model_loaded"]
        assert registry.health("cosyvoice3-local")["last_loaded_at"]
        response = client.get(f"/api/projects/{project}/tasks/{task['id']}/audio")
        assert response.status_code == 200 and response.content[:4] == b"RIFF"
    finally:
        client.__exit__(None, None, None)


def test_failed_retry_executes_old_archive_after_current_inputs_change(tmp_path: Path) -> None:
    client, directory, db, project, task, registry, control, _, location = setup_real(tmp_path)
    try:
        control.write_text("fail")
        with WorkerSession(directory, registry) as worker:
            assert worker.run_once()
        failed = task_state(db, project, task["id"])
        assert failed["state"] == "failed" and failed["error"]["code"] == "model_inference_failed"
        new_location, new_config = fixture_model(tmp_path / "new-model")
        assert new_location != location
        save_config(new_config, location)
        with connect(db) as connection:
            connection.execute("UPDATE text_documents SET revision=revision+1")
            connection.execute("DELETE FROM project_reference_selection")
        response = client.post(
            f"/api/projects/{project}/tasks/{task['id']}/retry", json={"request_id": str(uuid4())}
        )
        assert response.status_code == 201, response.text
        child = response.json()
        control.write_text("success")
        with WorkerSession(directory, registry) as worker:
            assert worker.run_once()
        assert_result(directory, task_state(db, project, child["id"]), task)
        assert task_state(db, project, task["id"]) == failed
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("behavior", ["block", "no-status"])
def test_timeout_reaps_bridge_then_next_task_succeeds(tmp_path: Path, behavior: str) -> None:
    client, directory, db, project, task, registry, control, _, _ = setup_real(tmp_path, 0.4)
    try:
        control.write_text(behavior)
        with WorkerSession(directory, registry) as worker:
            assert worker.run_once()
        failed = task_state(db, project, task["id"])
        assert failed["error"]["code"] == "model_timeout"
        pid = int(control.with_suffix(".pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        control.write_text("success")
        response = client.post(
            f"/api/projects/{project}/tasks/{task['id']}/retry", json={"request_id": str(uuid4())}
        )
        assert response.status_code == 201
        registry.adapters["cosyvoice3-local"].bridge_timeout = 10  # type: ignore[attr-defined]
        with WorkerSession(directory, registry) as worker:
            assert worker.run_once()
        assert_result(directory, task_state(db, project, response.json()["id"]), task)
    finally:
        client.__exit__(None, None, None)


def test_exact_token_boundary_uses_model_tokenizer() -> None:
    calls = []

    def encode(text: str, *, allowed_special: Any) -> list[int]:
        calls.append((text, allowed_special))
        return list(range(len(text)))

    model = SimpleNamespace(
        frontend=SimpleNamespace(
            tokenizer=SimpleNamespace(encode=encode),
            allowed_special={"fixture"},
        )
    )
    validate_text_tokens(model, ["x" * 80], 80)
    with pytest.raises(BridgeFailure, match="text_limit_exceeded"):
        validate_text_tokens(model, ["x" * 81], 80)
    assert calls == [("x" * 80, {"fixture"}), ("x" * 81, {"fixture"})]


def wait_file(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        assert time.monotonic() < deadline, str(path)
        time.sleep(0.02)


def test_no_status_and_stale_status_obey_load_deadline(tmp_path: Path) -> None:
    client, directory, db, project, task, registry, control, config, _ = setup_real(
        tmp_path, timeout=10, load_timeout=0.7
    )
    try:
        control.write_text("no-status")
        status = directory / "model-health" / f"{config['config_fingerprint']}.json"
        status.parent.mkdir()
        status.write_text(
            json.dumps(
                {
                    "task_id": "old-task",
                    "pid": os.getpid(),
                    "paragraph_index": 99,
                    "state": "completed",
                    "model_loaded": False,
                }
            )
        )
        started = time.monotonic()
        with WorkerSession(directory, registry) as worker:
            assert worker.run_once()
        assert time.monotonic() - started < 5
        assert task_state(db, project, task["id"])["error"]["code"] == "model_timeout"
        assert not list((directory / "generation-tmp").iterdir())
    finally:
        client.__exit__(None, None, None)


def test_worker_stop_reaps_running_bridge_before_releasing_lock(tmp_path: Path) -> None:
    client, directory, db, project, task, registry, control, _, _ = setup_real(tmp_path)
    try:
        control.write_text("block")
        with WorkerSession(directory, registry) as worker:
            thread = threading.Thread(target=worker.run_once)
            thread.start()
            wait_file(control.with_suffix(".pid"))
            pid = int(control.with_suffix(".pid").read_text())
            worker.stop.set()
            thread.join(timeout=5)
            assert not thread.is_alive()
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        assert task_state(db, project, task["id"])["error"]["code"] == "worker_interrupted"
    finally:
        client.__exit__(None, None, None)


def test_sigkill_worker_watchdog_reaps_bridge_before_recovery(tmp_path: Path) -> None:
    client, directory, db, project, task, registry, control, _, location = setup_real(tmp_path)
    process = None
    try:
        control.write_text("block")
        code = """
import sys
from pathlib import Path
from backend.app.generation import GenerationRegistry, SimulatedToneAdapter
from backend.app.local_model import LocalCosyVoiceAdapter
from backend.app.worker import WorkerSession
directory, config, runner, control = map(Path, sys.argv[1:])
adapter = LocalCosyVoiceAdapter(config,
    bridge_command=[sys.executable, str(runner), '--control', str(control)])
with WorkerSession(directory, GenerationRegistry([SimulatedToneAdapter(), adapter])) as worker:
    worker.run_once()
"""
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(directory),
                str(location),
                str(RUNNER),
                str(control),
            ]
        )
        wait_file(control.with_suffix(".pid"))
        pid = int(control.with_suffix(".pid").read_text())
        with pytest.raises(BlockingIOError):
            with WorkerSession(directory, registry):
                pass
        assert task_state(db, project, task["id"])["state"] == "running"
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        worker = WorkerSession(directory, registry)
        while True:
            try:
                worker.__enter__()
                break
            except BlockingIOError:
                assert time.monotonic() < deadline
                time.sleep(0.02)
        worker.__exit__(None, None, None)
        state = task_state(db, project, task["id"])
        assert state["state"] == "failed" and state["error"]["code"] == "worker_interrupted"
        # Reparented dead children may briefly be zombies; they cannot execute or hold flock.
        probe = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        )
        assert probe.returncode == 1 or probe.stdout.strip().startswith("Z"), probe.stdout
        assert not list((directory / "generation-tmp").glob("*"))
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        client.__exit__(None, None, None)


@pytest.mark.parametrize("sample", [float("nan"), float("inf"), -float("inf")])
def test_production_bridge_rejects_nonfinite_before_pcm_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample: float
) -> None:
    client, directory, _, _, task, _, _, config, _ = setup_real(tmp_path)
    old_cwd = Path.cwd()
    try:
        reference = task["snapshot"]["reference"]
        reference_path = directory / "references" / reference["id"] / "clips"
        reference_path /= f"{reference['clip']['id']}.wav"
        request = tmp_path / "request.json"
        request.write_text(
            json.dumps(
                {
                    "configuration": config,
                    "snapshot": task["snapshot"],
                    "task_id": task["id"],
                    "reference_path": str(reference_path),
                }
            )
        )
        calls = []

        class Samples:
            def detach(self) -> Any:
                return self

            def cpu(self) -> Any:
                return self

            def reshape(self, _: int) -> Any:
                return self

        class Engine:
            sample_rate = 24000
            frontend = SimpleNamespace(
                tokenizer=SimpleNamespace(encode=lambda *args, **kwargs: [1]),
                allowed_special=set(),
            )

            def inference_zero_shot(self, *args: Any, **kwargs: Any) -> Any:
                calls.append((args, kwargs))
                yield {"tts_speech": Samples()}

        torch = ModuleType("torch")
        torch.set_num_threads = lambda _: None  # type: ignore[attr-defined]
        torch.isfinite = lambda _: SimpleNamespace(  # type: ignore[attr-defined]
            all=lambda: math.isfinite(sample)
        )
        cosyvoice = ModuleType("cosyvoice.cli.cosyvoice")
        cosyvoice.AutoModel = lambda **kwargs: Engine()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "torch", torch)
        monkeypatch.setitem(sys.modules, "cosyvoice.cli.cosyvoice", cosyvoice)
        with pytest.raises(BridgeFailure, match="invalid_model_output"):
            run(
                SimpleNamespace(
                    input=request,
                    output=tmp_path / "output.wav",
                    progress=tmp_path / "progress.json",
                    status=tmp_path / "status.json",
                )
            )
        assert calls[0][0][0] == task["snapshot"]["confirmation"]["paragraphs"][0]["draft_text"]
        assert calls[0][0][1].endswith(reference["transcript"]["text"])
        assert Path(calls[0][0][2]).read_bytes() == reference_path.read_bytes()
        assert calls[0][1] == {"stream": False, "text_frontend": False, "speed": 1.0}
        assert not (tmp_path / "progress.json").exists()
        assert not (tmp_path / "output.meta.json").exists()
    finally:
        os.chdir(old_cwd)
        client.__exit__(None, None, None)


def test_dead_bridge_leader_descendant_is_reaped_before_next_task(tmp_path: Path) -> None:
    client, directory, db, project, task, registry, control, _, _ = setup_real(tmp_path)
    try:
        control.write_text("orphan")
        with WorkerSession(directory, registry) as worker:
            assert worker.run_once()
            child = int(control.with_suffix(".child-pid").read_text())
            probe = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child)],
                capture_output=True,
                text=True,
            )
            assert probe.returncode == 1 or probe.stdout.strip().startswith("Z"), probe.stdout
            failed = task_state(db, project, task["id"])
            assert failed["state"] == "failed"
            response = client.post(
                f"/api/projects/{project}/tasks/{task['id']}/retry",
                json={"request_id": str(uuid4())},
            )
            assert response.status_code == 201
            control.write_text("success")
            assert worker.run_once()
        assert_result(directory, task_state(db, project, response.json()["id"]), task)
    finally:
        client.__exit__(None, None, None)


def test_unverified_cleanup_stops_worker_and_preserves_next_queued_task(tmp_path: Path) -> None:
    client, directory, db, project, task, registry, _, _, _ = setup_real(tmp_path)
    real_stop = LocalCosyVoiceAdapter.stop_process

    def verification_failure(process: Any) -> None:
        # Reap actual owned children for test hygiene, then inject inability to verify cleanup.
        real_stop(process)
        raise TaskError(503, "model_cleanup_failed", "cleanup verification unavailable")

    try:
        with WorkerSession(directory, registry) as worker:
            with patch.object(
                LocalCosyVoiceAdapter, "stop_process", side_effect=verification_failure
            ):
                assert worker.run_once()
            failed = task_state(db, project, task["id"])
            assert failed["error"]["code"] == "model_cleanup_failed"
            assert failed["result"] is None and worker.stop.is_set()
            response = client.post(
                f"/api/projects/{project}/tasks/{task['id']}/retry",
                json={"request_id": str(uuid4())},
            )
            assert response.status_code == 201
            assert not worker.run_once()
            assert task_state(db, project, response.json()["id"])["state"] == "queued"
        with WorkerSession(directory, registry) as replacement:
            assert replacement.run_once()
        assert_result(directory, task_state(db, project, response.json()["id"]), task)
    finally:
        client.__exit__(None, None, None)


def test_real_bridge_allows_short_nonempty_segment_and_keeps_whole_pcm(tmp_path: Path) -> None:
    client, directory, db, project, task, registry, control, _, _ = setup_real(tmp_path, full=True)
    try:
        control.write_text("short")
        with WorkerSession(directory, registry) as worker:
            assert worker.run_once()
        finished = task_state(db, project, task["id"])
        assert_result(directory, finished, task)
        assert [part["frame_count"] for part in finished["result"]["segments"]] == [24, 6000]
        assert finished["result"]["frame_count"] == 6024
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("fault", [None, "empty-second", "fail-second"])
def test_production_bridge_multichunk_paragraph_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import struct

    from backend.app.tasks import file_digest

    client, directory, _, _, task, _, _, config, _ = setup_real(tmp_path, full=True)
    old_cwd = Path.cwd()
    try:
        ref = task["snapshot"]["reference"]
        ref_path = directory / "references" / ref["id"] / "clips" / f"{ref['clip']['id']}.wav"
        request = tmp_path / "request.json"
        request.write_text(
            json.dumps(
                {
                    "configuration": config,
                    "snapshot": task["snapshot"],
                    "task_id": task["id"],
                    "reference_path": str(ref_path),
                }
            )
        )
        calls: list[Any] = []
        chunks = [[11, 12], [13, 14, 15], [21, 22, 23, 24]]

        class Samples:
            def __init__(self, values: list[int]) -> None:
                self.values = values

            def detach(self) -> Any:
                return self

            def cpu(self) -> Any:
                return self

            def reshape(self, _: int) -> Any:
                return self

            def numel(self) -> int:
                return len(self.values)

            def clamp(self, *_: Any) -> Any:
                return self

            def __mul__(self, _: Any) -> Any:
                return self

            def to(self, _: Any) -> Any:
                return self

            def numpy(self) -> Any:
                return self

            def astype(self, _: Any) -> Any:
                return self

            def tobytes(self) -> bytes:
                return struct.pack("<" + "h" * len(self.values), *self.values)

        class Engine:
            sample_rate = 24000
            frontend = SimpleNamespace(
                tokenizer=SimpleNamespace(encode=lambda *a, **k: [1]), allowed_special=set()
            )

            def inference_zero_shot(self, *args: Any, **kwargs: Any) -> Any:
                calls.append((args, kwargs))
                if len(calls) == 2 and fault == "fail-second":
                    raise RuntimeError("fixture second paragraph failed")
                selected = chunks[:2] if len(calls) == 1 else ([] if fault else chunks[2:])
                for values in selected:
                    yield {"tts_speech": Samples(values)}

        torch = ModuleType("torch")
        torch.set_num_threads = lambda _: None  # type: ignore[attr-defined]
        torch.isfinite = lambda _: SimpleNamespace(all=lambda: True)  # type: ignore[attr-defined]
        torch.int16 = "int16"  # type: ignore[attr-defined]
        cosyvoice = ModuleType("cosyvoice.cli.cosyvoice")
        cosyvoice.AutoModel = lambda **kwargs: Engine()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "torch", torch)
        monkeypatch.setitem(sys.modules, "cosyvoice.cli.cosyvoice", cosyvoice)
        args = SimpleNamespace(
            input=request,
            output=tmp_path / "native.wav",
            progress=tmp_path / "progress.json",
            status=tmp_path / "status.json",
        )
        if fault:
            with pytest.raises((BridgeFailure, RuntimeError)):
                run(args)
            assert not args.output.with_suffix(".meta.json").exists()
            assert json.loads(args.progress.read_text())["completed_paragraphs"] == 1
            return
        run(args)
        meta = json.loads(args.output.with_suffix(".meta.json").read_text())
        parts = meta["segments"]
        assert [p["paragraph_id"] for p in parts] == task["snapshot"]["paragraph_ids"]
        assert [p["frame_count"] for p in parts] == [5, 4]
        pcm = b""
        for index, part in enumerate(parts):
            path = tmp_path / "native-segments" / f"{part['paragraph_id']}.wav"
            assert file_digest(path) == part["sha256"]
            with wave.open(str(path)) as audio:
                raw = audio.readframes(audio.getnframes())
            assert raw == Samples([11, 12, 13, 14, 15] if index == 0 else chunks[2]).tobytes()
            pcm += raw
        with wave.open(str(args.output)) as audio:
            assert audio.readframes(audio.getnframes()) == pcm
        assert [c[0][0] for c in calls] == [
            p["draft_text"] for p in task["snapshot"]["confirmation"]["paragraphs"]
        ]
        assert all(c[1] == {"stream": False, "text_frontend": False, "speed": 1.0} for c in calls)
    finally:
        os.chdir(old_cwd)
        client.__exit__(None, None, None)
