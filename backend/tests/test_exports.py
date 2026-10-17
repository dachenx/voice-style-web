import wave
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.tests.test_segments import full_task
from backend.tests.test_versions import choose, edit, history


def setup(tmp_path):
    _, project, _, task = full_task(tmp_path)
    client = TestClient(create_app(tmp_path, worker_enabled=False))
    client.__enter__()
    for index in range(2):
        assert choose(client, project, history(client, project), index).status_code == 200
    return client, project, task


def payload(client, project, **kwargs):
    c = history(client, project)
    return dict(
        request_id=str(uuid4()),
        adapter_id="simulated-tone",
        comparison_token=c["comparison_token"],
        selections={
            p["id"]: p["selection"]["revision"] if p["selection"] else 0 for p in c["paragraphs"]
        },
        **kwargs,
    )


def pcm(raw):
    with wave.open(BytesIO(raw)) as audio:
        return audio.readframes(audio.getnframes())


def test_exact_order_pause_single_idempotence_and_restart(tmp_path):
    client, project, task = setup(tmp_path)
    base = f"/api/projects/{project}/exports"
    try:
        body = payload(client, project, pause_ms=500)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post(base, json=body), range(2)))
        assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
        data = responses[0].json()
        raw = client.get(f"{base}/{data['id']}/audio").content
        sources = [
            pcm((tmp_path / "generation-results" / task["id"] / s["file"]).read_bytes())
            for s in task["result"]["segments"]
        ]
        assert pcm(raw) == sources[0] + b"\0\0" * 12000 + sources[1]
        assert data["segments"][1]["start_frame"] == len(sources[0]) // 2 + 12000
        assert data["is_simulated"]
        assert len(client.get(base).json()) == 1
        assert client.post(base, json={**body, "pause_ms": 0}).status_code == 409
        single = client.post(
            base,
            json=payload(
                client, project, paragraph_id=data["segments"][0]["paragraph_id"], pause_ms=1000
            ),
        ).json()
        assert pcm(client.get(f"{base}/{single['id']}/audio").content) == sources[0]
        other = client.post("/api/projects", json={"name": "other"}).json()["id"]
        assert client.get(f"/api/projects/{other}/exports/{data['id']}/audio").status_code == 409
        edit(client, project, "text")
        assert client.post(base, json=body).json()["id"] == data["id"]
        assert client.get(f"{base}/{data['id']}/audio").content == raw
    finally:
        client.__exit__(None, None, None)
    orphan = tmp_path / "exports" / project / ".pending-interrupted"
    orphan.mkdir()
    (orphan / "audio.wav").write_bytes(b"partial")
    with TestClient(create_app(tmp_path, worker_enabled=False)) as restarted:
        assert not orphan.exists()
        assert len(restarted.get(base).json()) == 2
        assert restarted.get(f"{base}/{data['id']}/audio").content == raw
        assert restarted.get(f"{base}/{data['id']}/manifest").json()["id"] == data["id"]


def test_conflicts_unconfirmed_stale_missing_and_validation(tmp_path):
    client, project, task = setup(tmp_path)
    base = f"/api/projects/{project}/exports"
    try:
        old = payload(client, project)
        assert choose(client, project, history(client, project)).status_code == 200
        assert client.post(base, json=old).json()["error"]["code"] == "export_inputs_changed"
        edit(client, project, "text")
        assert (
            client.post(base, json=payload(client, project)).json()["error"]["code"]
            == "export_unconfirmed"
        )
        doc = client.get(f"/api/projects/{project}/text").json()
        client.post(
            f"/api/projects/{project}/text/confirm",
            json={"expected_revision": doc["revision"], "acknowledged": True},
        )
        assert (
            client.post(base, json=payload(client, project)).json()["error"]["code"]
            == "export_acknowledgement"
        )
        result = client.post(base, json=payload(client, project, acknowledge_mismatch=True))
        assert result.status_code == 200, result.text
        for value in [-1, 2001, True, 0.5]:
            assert (
                client.post(base, json=payload(client, project, pause_ms=value)).status_code == 422
            )
        path = tmp_path / "generation-results" / task["id"] / task["result"]["segments"][0]["file"]
        path.unlink()
        assert (
            client.post(base, json=payload(client, project, acknowledge_mismatch=True)).json()[
                "error"
            ]["code"]
            == "export_missing"
        )
        assert len(client.get(base).json()) == 1
    finally:
        client.__exit__(None, None, None)


def test_write_failure_keeps_old_export_and_corruption_reported(tmp_path):
    client, project, _ = setup(tmp_path)
    base = f"/api/projects/{project}/exports"
    try:
        first = client.post(base, json=payload(client, project)).json()
        with patch("backend.app.exports.os.replace", side_effect=OSError("disk failure")):
            r = client.post(base, json=payload(client, project))
        assert r.status_code == 409
        root = tmp_path / "exports" / project
        assert not list(root.glob(".pending-*"))
        assert len(client.get(base).json()) == 1
        (root / first["id"] / "audio.wav").write_bytes(b"bad")
        assert client.get(base).json()[0]["available"] is False
        assert client.get(f"{base}/{first['id']}/audio").status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_manifest_corruption_and_strict_revisions(tmp_path):
    import json

    client, project, _ = setup(tmp_path)
    base = f"/api/projects/{project}/exports"
    try:
        for invalid in [True, 1.0, "1"]:
            body = payload(client, project)
            key = next(iter(body["selections"]))
            body["selections"][key] = invalid
            assert client.post(base, json=body).status_code == 422
        result = client.post(base, json=payload(client, project)).json()
        path = tmp_path / "exports" / project / result["id"] / "manifest.json"
        value = json.loads(path.read_text())
        value["segments"] = [None]
        path.write_text(json.dumps(value))
        assert client.get(base).json() == [{"id": result["id"], "available": False}]
        assert client.get(f"{base}/{result['id']}/manifest").status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_slow_audio_write_does_not_lock_database_and_snapshot_is_frozen(tmp_path):
    from threading import Event

    from backend.app.storage import connect

    client, project, _ = setup(tmp_path)
    base = f"/api/projects/{project}/exports"
    entered, release = Event(), Event()
    original = __import__("os").fsync

    def slow(fd):
        entered.set()
        assert release.wait(5)
        original(fd)

    try:
        body = payload(client, project)
        with patch("backend.app.exports.os.fsync", side_effect=slow), ThreadPoolExecutor() as pool:
            pending = pool.submit(client.post, base, json=body)
            try:
                assert entered.wait(5)
                with connect(tmp_path / "projects.sqlite3") as connection:
                    connection.execute("UPDATE paragraph_audio_selections SET revision=revision+1")
            finally:
                release.set()
            result = pending.result()
        assert result.status_code == 200, result.text
        assert all(s["selection"]["revision"] == 1 for s in result.json()["segments"])
        assert all(p["selection"]["revision"] == 2 for p in history(client, project)["paragraphs"])
    finally:
        client.__exit__(None, None, None)


def test_real_process_exit_before_and_after_atomic_publish(tmp_path):
    import json
    import subprocess
    import sys

    client, project, _ = setup(tmp_path)
    body = payload(client, project)
    client.__exit__(None, None, None)
    base = f"/api/projects/{project}/exports"
    script = """
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend.app.main import create_app
root, route, body, after = sys.argv[1:]
original = os.replace
def exit_at_publish(source, target):
    if after == 'yes': original(source, target)
    os._exit(77)
with TestClient(create_app(Path(root), worker_enabled=False)) as client:
    with patch('backend.app.exports.os.replace', side_effect=exit_at_publish):
        client.post(route, json=json.loads(body))
"""
    for after in ["no", "yes"]:
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), base, json.dumps(body), after],
            timeout=15,
            capture_output=True,
        )
        assert result.returncode == 77, result.stderr
        with TestClient(create_app(tmp_path, worker_enabled=False)) as restarted:
            records = restarted.get(base).json()
            assert len(records) == (1 if after == "yes" else 0)
            assert not list((tmp_path / "exports" / project).glob(".pending-*"))
            if after == "yes":
                assert restarted.post(base, json=body).json()["id"] == body["request_id"]
                assert restarted.get(f"{base}/{body['request_id']}/audio").status_code == 200


def test_replay_verification_does_not_hold_database_read_lock(tmp_path):
    from threading import Event

    from backend.app.exports import verified_audio
    from backend.app.storage import connect

    client, project, _ = setup(tmp_path)
    base = f"/api/projects/{project}/exports"
    body = payload(client, project)
    assert client.post(base, json=body).status_code == 200
    entered, release = Event(), Event()

    def slow(folder, manifest):
        entered.set()
        assert release.wait(5)
        return verified_audio(folder, manifest)

    try:
        with (
            patch("backend.app.exports.verified_audio", side_effect=slow),
            ThreadPoolExecutor() as pool,
        ):
            pending = pool.submit(client.post, base, json=body)
            try:
                assert entered.wait(5)
                with connect(tmp_path / "projects.sqlite3") as connection:
                    connection.execute("UPDATE paragraph_audio_selections SET revision=revision+1")
            finally:
                release.set()
            assert pending.result().status_code == 200
        assert len(client.get(base).json()) == 1
    finally:
        client.__exit__(None, None, None)
