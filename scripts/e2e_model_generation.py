"""Disposable browser fixture for the real adapter's software contract only.

Synthetic model files and a trusted subprocess runner replace expensive model
inference. API, queue, WorkerSession, bridge supervision and WAV publication are
production code. No user configuration, recording, model or test HTTP route is used.
"""

import argparse
import hashlib
import io
import json
import math
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.model_config import (  # noqa: E402
    REQUIRED_MODEL_FILES,
    build_config,
    canonical_config,
    digest_file,
    digest_json,
    save_config,
    validate_config,
)
from scripts.cosyvoice_bridge import install_parent_watchdog, write_json  # noqa: E402


def synthetic_wave(frames: int = 24000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"".join(
            struct.pack("<h", round(4000 * math.sin(2 * math.pi * 400 * index / 24000)))
            for index in range(frames)
        ))
    return output.getvalue()


def configuration(directory: Path) -> tuple[Path, Path]:
    repository = directory / "synthetic-repository"
    (repository / "cosyvoice/cli").mkdir(parents=True)
    (repository / "cosyvoice/cli/cosyvoice.py").write_text("# Browser fixture only\n")
    (repository / "third_party/Matcha-TTS/matcha").mkdir(parents=True)
    (repository / "third_party/Matcha-TTS/matcha/__init__.py").write_text("# Fixture\n")
    model = repository / "model"
    for name in REQUIRED_MODEL_FILES:
        path = model / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("Synthetic browser fixture: " + name).encode())
    config = build_config(Path(sys.executable), repository, model)
    path = directory / "private/model-runtime.json"
    save_config(config, path)
    return path, model / "llm.pt"


def runner(arguments: list[str]) -> int:
    # Use the production parent-pipe watcher and process group policy verbatim.
    install_parent_watchdog()
    parser = argparse.ArgumentParser()
    for name in ("input", "output", "progress", "status", "control-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(arguments)
    envelope = json.loads(args.input.read_text(encoding="utf-8"))
    config, snapshot = envelope["configuration"], envelope["snapshot"]
    validate_config(config, full_hash=True)
    reference_sha = digest_file(Path(envelope["reference_path"]))
    assert reference_sha == snapshot["reference"]["clip_sha256"]
    paragraphs = {value["id"]: value for value in snapshot["confirmation"]["paragraphs"]}
    texts = [paragraphs[identifier]["draft_text"] for identifier in snapshot["paragraph_ids"]]
    now = datetime.now(UTC).isoformat()
    status = {
        "pid": os.getpid(), "task_id": envelope["task_id"], "state": "loaded",
        "model_loaded": True, "loaded_at": now,
        "config_fingerprint": config["config_fingerprint"],
    }
    write_json(args.status, status)
    # Files are a test-only process barrier/evidence channel, never an app endpoint.
    write_json(args.control_dir / f"{envelope['task_id']}.json", {
        "scope": "synthetic bridge software fixture", "pid": os.getpid(),
        "task_id": envelope["task_id"], "texts": texts,
        "transcript": snapshot["reference"]["transcript"],
        "reference_sha256": reference_sha, "config_fingerprint": config["config_fingerprint"],
        "snapshot": snapshot,
    })
    deadline = time.monotonic() + 25
    while (args.control_dir / "hold").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Browser fixture barrier was not released")
        threading.Event().wait(0.02)
    failure = args.control_dir / "fail-next"
    if failure.exists():
        failure.unlink()
        write_json(args.output.with_suffix(".error.json"), {"code": "model_inference_failed"})
        return 1
    with wave.open(str(args.output), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        for index, _text in enumerate(texts, 1):
            status.update(state="generating", paragraph_index=index)
            write_json(args.status, status)
            with wave.open(io.BytesIO(synthetic_wave(6000)), "rb") as source:
                audio.writeframes(source.readframes(6000))
            write_json(args.progress, {"completed_paragraphs": index})
    write_json(args.output.with_suffix(".meta.json"), {
        "input_sha256": hashlib.sha256(canonical_config(snapshot).encode()).hexdigest(),
        "reference_sha256": reference_sha,
        "config_fingerprint": config["config_fingerprint"],
        "model_fingerprint": config["model_fingerprint"],
        "environment_fingerprint": digest_json(config["environment"]),
        "native_sample_rate": 24000, "frame_count": 6000 * len(texts),
        "model_loaded_at": now,
    })
    status.update(state="completed", model_loaded=False)
    write_json(args.status, status)
    return 0


def adapters(directory: Path) -> list[Any]:
    from backend.app.generation import SimulatedToneAdapter
    from backend.app.local_model import LocalCosyVoiceAdapter

    return [SimulatedToneAdapter(), LocalCosyVoiceAdapter(
        directory / "private/model-runtime.json",
        runtime_directory=directory / "data/model-health",
        bridge_command=[sys.executable, str(Path(__file__).resolve()), "--runner",
                        "--control-dir", str(directory / "control")],
        bridge_timeout=30,
    )]


def worker(directory: Path) -> int:
    from backend.app.generation import GenerationRegistry
    from backend.app.worker import WorkerSession

    session = WorkerSession(directory / "data", GenerationRegistry(adapters(directory)))

    def stop(signum: int, frame: FrameType | None) -> None:
        session.stop.set()

    def parent_closed() -> None:
        sys.stdin.buffer.read(1)
        session.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=parent_closed, daemon=True).start()
    with session:
        while not session.stop.is_set():
            if not session.run_once():
                session.stop.wait(0.05)
    return 0


def prepare(client: Any, name: str) -> dict[str, Any]:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201, response.text
    project = response.json()
    base = f"/api/projects/{project['id']}"
    response = client.put(f"{base}/text", json={
        "expected_revision": 0, "source_language": "zh-CN", "target_language": "wuhan",
        "register": "自然口语", "terms": "", "paragraphs": [
            {"id": str(uuid4()), "original_text": f"原始人工流程文字第{index}段。",
             "draft_text": f"确认的人工流程文字第{index}段，金额 {index}23 元。",
             "pronunciation_note": "软件验证，不评价语音"} for index in (1, 2)
        ],
    })
    assert response.status_code == 200, response.text
    response = client.post(f"{base}/text/confirm", json={
        "expected_revision": response.json()["revision"], "acknowledged": True,
    })
    assert response.status_code == 200, response.text
    document = response.json()
    response = client.post(f"{base}/references", params={"filename": "synthetic-model.wav"},
                           content=synthetic_wave(),
                           headers={"content-type": "application/octet-stream"})
    assert response.status_code == 201, response.text
    reference_id = response.json()["id"]
    response = client.post(f"{base}/references/{reference_id}/clips", json={
        "start_ms": 0, "end_ms": 800,
    })
    assert response.status_code == 201, response.text
    clip_id = response.json()["id"]
    url = f"{base}/references/{reference_id}/clips/{clip_id}/transcript"
    response = client.get(url)
    assert response.status_code == 200, response.text
    response = client.put(url, json={
        "expected_revision": 0, "expected_clip_sha256": response.json()["clip_sha256"],
        "text": "人工编写的参考原话，只用于软件链路。",
    })
    assert response.status_code == 200, response.text
    return {"project": project, "document": document, "reference_id": reference_id,
            "clip_id": clip_id, "transcript": response.json()}


def serve(directory: Path, unconfigured: bool) -> int:
    # Also isolate the module-level application's lazy configuration construction.
    os.environ["VOICE_DATA_DIR"] = str(directory / "data")
    os.environ["VOICE_MODEL_CONFIG"] = str(directory / "private/model-runtime.json")
    import uvicorn
    from fastapi.testclient import TestClient

    from backend.app.main import create_app

    if not directory.is_dir() or any(directory.iterdir()):
        raise ValueError("Model browser fixture requires an empty temporary directory")
    (directory / "control").mkdir()
    model_file = None
    if not unconfigured:
        _, model_file = configuration(directory)
    application = create_app(directory / "data", worker_enabled=False,
                             generation_adapters=adapters(directory),
                             model_config_path=directory / "private/model-runtime.json")
    with TestClient(application) as client:
        projects = {name: prepare(client, f"模型流程夹具 {name}")
                    for name in ("gates", "playback", "replay", "failure", "configuration")}
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker", str(directory)],
        stdin=subprocess.PIPE, stdout=sys.stderr, stderr=sys.stderr,
    )
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(application, log_level="warning"))

            def parent_closed() -> None:
                sys.stdin.buffer.read()
                server.should_exit = True

            threading.Thread(target=parent_closed, daemon=True).start()
            print(json.dumps({"origin": origin, "projects": projects,
                              "model_file": str(model_file) if model_file else None}), flush=True)
            server.run(sockets=[listener])
    finally:
        assert process.stdin is not None
        process.stdin.close()
        try:
            code = process.wait(timeout=7)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            raise
        if code != 0:
            raise RuntimeError(f"Fixture worker exited with {code}")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--runner":
        return runner(sys.argv[2:])
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        return worker(Path(sys.argv[2]).resolve())
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--unconfigured", action="store_true")
    args = parser.parse_args()
    return serve(args.directory.resolve(), args.unconfigured)


if __name__ == "__main__":
    raise SystemExit(main())
