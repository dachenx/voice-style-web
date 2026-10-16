"""Disposable real API fixture: one genuine adapter failure and one queued task.

Only browser acceptance launches this script. No test route or state override is
added to the application. The caller owns the empty temporary directory and its
cleanup; closing stdin gracefully stops the server, including on Windows.
"""

import argparse
import io
import json
import math
import socket
import struct
import sys
import threading
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.generation import GenerationRegistry, SimulatedToneAdapter  # noqa: E402
from backend.app.main import create_app  # noqa: E402
from backend.app.worker import WorkerSession  # noqa: E402


class FailingAdapter(SimulatedToneAdapter):
    """Keep the real simulated capability; fail inside its generation contract."""

    def generate(
        self, snapshot: dict[str, Any], destination: Path, progress: Callable[[int], None]
    ) -> None:
        raise RuntimeError("intentional browser recovery fixture failure")


def source_wave() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(b"".join(
            struct.pack("<h", round(4000 * math.sin(2 * math.pi * 400 * frame / 24000)))
            for frame in range(24000)
        ))
    return output.getvalue()


def prepare(client: TestClient, name: str) -> dict[str, str]:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]
    base = f"/api/projects/{project_id}"
    response = client.put(f"{base}/text", json={
        "expected_revision": 0,
        "source_language": "zh-CN", "target_language": "wuhan",
        "register": "自然口语", "terms": "",
        "paragraphs": [{
            "id": str(uuid4()), "original_text": "恢复测试原稿，金额 123 元。",
            "draft_text": "恢复测试已确认草稿，金额 123 元。", "pronunciation_note": "测试备注",
        }],
    })
    assert response.status_code == 200, response.text
    response = client.post(f"{base}/text/confirm", json={
        "expected_revision": response.json()["revision"], "acknowledged": True,
    })
    assert response.status_code == 200, response.text
    document = response.json()
    response = client.post(
        f"{base}/references", params={"filename": "synthetic-recovery.wav"},
        content=source_wave(), headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 201, response.text
    reference_id = response.json()["id"]
    response = client.post(f"{base}/references/{reference_id}/clips", json={
        "start_ms": 0, "end_ms": 800,
    })
    assert response.status_code == 201, response.text
    response = client.post(f"{base}/tasks", json={
        "request_id": str(uuid4()), "expected_revision": document["revision"],
        "expected_confirmation_id": document["latest_confirmation"]["id"],
        "expected_clip_id": response.json()["id"], "adapter_id": "simulated-tone",
        "mode": "full", "paragraph_id": None, "parameters": {},
    })
    assert response.status_code == 201, response.text
    return {"project_id": project_id, "task_id": response.json()["id"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    directory = parser.parse_args().data_dir.resolve()
    if not directory.is_dir() or any(directory.iterdir()):
        raise ValueError("Recovery fixture requires an existing empty temporary directory")
    adapter = FailingAdapter()
    application = create_app(directory, worker_enabled=False, generation_adapters=[adapter])
    with TestClient(application) as client:
        failed = prepare(client, "真实失败恢复夹具")
        with WorkerSession(directory, GenerationRegistry([adapter])) as worker:
            assert worker.run_once(), "No queued fixture task was claimed"
        response = client.get(f"/api/projects/{failed['project_id']}/tasks/{failed['task_id']}")
        assert response.status_code == 200, response.text
        task = response.json()
        assert task["state"] == "failed" and task["error"]["code"] == "generation_failed"
        assert task["started_at"] and task["finished_at"] and task["result"] is None
        queued = prepare(client, "真实排队取消夹具")

    # Reserve the listening socket directly; no race between probing and binding a port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(application, log_level="warning"))

        def stop_on_parent_exit() -> None:
            sys.stdin.buffer.read()
            server.should_exit = True

        threading.Thread(target=stop_on_parent_exit, daemon=True).start()
        print(json.dumps({"origin": origin, "failed": failed, "queued": queued}), flush=True)
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
