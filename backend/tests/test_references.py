import hashlib
import io
import struct
import subprocess
import threading
import wave
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.audio import MAX_BYTES, decode, ffmpeg_executable
from backend.app.main import create_app
from backend.app.storage import connect, migrate
from backend.tests.test_api import server


def wav_bytes(
    duration_ms: int = 1000, sample_rate: int = 24000, *, varying: bool = False
) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = duration_ms * sample_rate // 1000
        samples = (
            b"".join(struct.pack("<h", (index % 32000) - 16000) for index in range(frames))
            if varying
            else b"\x01\x00" * frames
        )
        output.writeframes(samples)
    return buffer.getvalue()


@contextmanager
def project_client(directory: Path) -> Iterator[tuple[TestClient, str]]:
    with TestClient(create_app(directory)) as client:
        project = client.post("/api/projects", json={"name": "参考音频测试"}).json()
        yield client, f"/api/projects/{project['id']}"


def upload(
    client: TestClient, base: str, content: bytes, filename: str = "人工样音.wav"
) -> httpx.Response:
    return client.post(
        f"{base}/references",
        params={"filename": filename},
        content=content,
        headers={"content-type": "application/octet-stream"},
    )


def test_original_clips_ranges_and_selection(tmp_path: Path) -> None:
    original = wav_bytes(sample_rate=44100, varying=True)
    with project_client(tmp_path) as (client, base):
        response = upload(client, base, original)
        assert response.status_code == 201, response.text
        reference = response.json()
        assert reference["duration_ms"] == 1000
        assert reference["byte_size"] == len(original)
        assert reference["sha256"] == hashlib.sha256(original).hexdigest()
        assert reference["filename"] == "人工样音.wav"
        assert reference["clips"] == []
        assert client.get(f"{base}/references").json()["active_clip_id"] is None
        location = f"{base}/references/{reference['id']}"
        assert client.get(f"{location}/original").content == original
        preview = client.get(f"{location}/preview")
        with wave.open(io.BytesIO(preview.content), "rb") as audio:
            assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (
                24000,
                1,
                2,
            )
            assert audio.getnframes() == 24000
            preview_pcm = audio.readframes(24000)
        partial = client.get(f"{location}/preview", headers={"Range": "bytes=0-43"})
        assert partial.status_code == 206
        assert partial.content == preview.content[:44]
        assert partial.headers["content-range"].startswith("bytes 0-43/")
        first = client.post(f"{location}/clips", json={"start_ms": 100, "end_ms": 400})
        assert first.status_code == 201, first.text
        first_clip = first.json()
        first_audio = client.get(f"{location}/clips/{first_clip['id']}/audio").content
        with wave.open(io.BytesIO(first_audio), "rb") as audio:
            assert audio.getnframes() == 7200
            # Compare the requested 100–400 ms, not merely the output duration.
            expected_pcm = preview_pcm[2400 * 2 : 9600 * 2]
            assert expected_pcm != preview_pcm[:7200 * 2]
            assert audio.readframes(7200) == expected_pcm
        second = client.post(f"{location}/clips", json={"start_ms": 0, "end_ms": 1000}).json()
        assert second["id"] != first_clip["id"]
        second_audio = client.get(f"{location}/clips/{second['id']}/audio").content
        with wave.open(io.BytesIO(second_audio), "rb") as audio:
            assert audio.readframes(audio.getnframes()) == preview_pcm
        listing = client.get(f"{base}/references").json()
        assert listing["active_clip_id"] == second["id"]
        assert len(listing["references"][0]["clips"]) == 2
        assert [item["id"] for item in listing["references"][0]["clips"]] == [
            second["id"],
            first_clip["id"],
        ]
        assert client.get(f"{location}/clips/{first_clip['id']}/audio").content == first_audio
        assert client.put(f"{base}/active-clip", json={"clip_id": first_clip["id"]}).json() == {
            "active_clip_id": first_clip["id"]
        }
        assert client.get(f"{location}/original").content == original
        again = upload(client, base, original).json()
        assert again["id"] != reference["id"]
        before_restart = client.get(f"{base}/references").json()
        assert [item["id"] for item in before_restart["references"]] == [
            again["id"],
            reference["id"],
        ]
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"{base}/references").json() == before_restart
        assert client.get(f"{base}/references").json()["active_clip_id"] == first_clip["id"]
        assert client.get(f"{location}/clips/{first_clip['id']}/audio").content == first_audio


@pytest.mark.parametrize(
    "suffix,codec", [("mp3", "libmp3lame"), ("m4a", "aac"), ("flac", "flac"), ("ogg", "libvorbis")]
)
def test_actual_compressed_formats(tmp_path: Path, suffix: str, codec: str) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(wav_bytes())
    encoded = tmp_path / f"encoded.{suffix}"
    subprocess.run(
        [
            ffmpeg_executable(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-c:a",
            codec,
            str(encoded),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    with project_client(tmp_path / "data") as (client, base):
        response = upload(client, base, encoded.read_bytes(), f"sample.{suffix}")
        assert response.status_code == 201, response.text
        reference = response.json()
        assert 900 <= reference["duration_ms"] <= 1100
        assert (
            client.get(f"{base}/references/{reference['id']}/original").content
            == encoded.read_bytes()
        )


@pytest.mark.parametrize(
    "content,filename",
    [
        (b"bad", "bad.wav"),
        (b"", "empty.mp3"),
        (wav_bytes(99), "short.wav"),
        (b"#EXTM3U\nhttp://example.invalid/x.mp3", "fake.mp3"),
        (wav_bytes(), "fake.mp3"),
        (wav_bytes(), "unsupported.txt"),
        (wav_bytes(), "../path.wav"),
    ],
    ids=["invalid-bytes", "empty", "too-short", "playlist", "disguised-wav", "unsupported", "path"],
)
def test_bad_uploads_leave_no_record_or_file(tmp_path: Path, content: bytes, filename: str) -> None:
    with project_client(tmp_path) as (client, base):
        response = upload(client, base, content, filename)
        assert response.status_code == 422, response.text
        assert "error" in response.json()
        assert client.get(f"{base}/references").json() == {"references": [], "active_clip_id": None}
        assert not list((tmp_path / "audio-tmp").glob("*"))
        assert not list((tmp_path / "references").glob("*"))


def test_long_audio_and_byte_limit(tmp_path: Path) -> None:
    with project_client(tmp_path) as (client, base):
        assert upload(client, base, wav_bytes(600100)).status_code == 422
        # Deliberately lie about Content-Length: actual received bytes are the limit source.
        response = client.post(
            f"{base}/references?filename=large.wav",
            content=b"x" * (MAX_BYTES + 1),
            headers={"content-type": "application/octet-stream", "content-length": "1"},
        )
        assert response.status_code == 413
        assert client.get(f"{base}/references").json()["references"] == []
        assert not list((tmp_path / "audio-tmp").glob("*"))


@pytest.mark.parametrize(
    "start,end",
    [
        (-1, 200),
        (0, 99),
        (100, 100),
        (200, 100),
        (0, 1001),
        (0.1, 500),
        (0, 500.0),
        (True, 500),
        ("0", 500),
        (None, 500),
    ],
)
def test_invalid_clips_preserve_active(tmp_path: Path, start: object, end: object) -> None:
    with project_client(tmp_path) as (client, base):
        reference = upload(client, base, wav_bytes()).json()
        location = f"{base}/references/{reference['id']}"
        active = client.post(f"{location}/clips", json={"start_ms": 0, "end_ms": 100}).json()
        response = client.post(f"{location}/clips", json={"start_ms": start, "end_ms": end})
        assert response.status_code == 422
        listing = client.get(f"{base}/references").json()
        assert listing["active_clip_id"] == active["id"]
        assert len(listing["references"][0]["clips"]) == 1


def test_all_ownership_checks(tmp_path: Path) -> None:
    with project_client(tmp_path) as (client, base):
        other = client.post("/api/projects", json={"name": "另一个项目"}).json()
        other_base = f"/api/projects/{other['id']}"
        reference = upload(client, base, wav_bytes()).json()
        location = f"{base}/references/{reference['id']}"
        clip = client.post(f"{location}/clips", json={"start_ms": 0, "end_ms": 100}).json()
        wrong = f"{other_base}/references/{reference['id']}"
        for suffix in ("original", "preview", f"clips/{clip['id']}/audio"):
            assert client.get(f"{wrong}/{suffix}").status_code == 404
        assert client.post(f"{wrong}/clips", json={"start_ms": 0, "end_ms": 100}).status_code == 404
        assert (
            client.put(f"{other_base}/active-clip", json={"clip_id": clip["id"]}).status_code == 404
        )
        assert client.get(f"/api/projects/{uuid4()}/references").status_code == 404
        assert upload(client, f"/api/projects/{uuid4()}", wav_bytes()).status_code == 404
        different = upload(client, base, wav_bytes()).json()
        assert (
            client.get(f"{base}/references/{different['id']}/clips/{clip['id']}/audio").status_code
            == 404
        )


def test_decoder_timeout_and_storage_failure_preserve_existing(tmp_path: Path) -> None:
    with project_client(tmp_path) as (client, base):
        reference = upload(client, base, wav_bytes()).json()
        location = f"{base}/references/{reference['id']}"
        clip = client.post(f"{location}/clips", json={"start_ms": 0, "end_ms": 100}).json()
        before = client.get(f"{base}/references").json()
        with patch(
            "backend.app.audio.imageio_ffmpeg.get_ffmpeg_exe",
            side_effect=RuntimeError("private executable path"),
        ):
            response = upload(client, base, wav_bytes())
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "audio_dependency_unavailable"
            assert "private executable path" not in response.text
        with patch(
            "backend.app.audio.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 45)
        ):
            assert upload(client, base, wav_bytes()).status_code == 422
        with patch("backend.app.references.decode", side_effect=OSError("private disk path")):
            response = upload(client, base, wav_bytes())
            assert response.status_code == 503
            assert "private disk path" not in response.text
        with patch("backend.app.references.excerpt", side_effect=OSError("disk full")):
            assert (
                client.post(f"{location}/clips", json={"start_ms": 100, "end_ms": 300}).status_code
                == 503
            )
        assert client.get(f"{base}/references").json() == before
        assert client.get(f"{location}/clips/{clip['id']}/audio").status_code == 200
        assert not list((tmp_path / "audio-tmp").glob("*"))
        assert len(list((tmp_path / "references").glob("*"))) == 1
        assert len(list((tmp_path / "references" / reference["id"] / "clips").glob("*"))) == 1


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_ffmpeg_execution_failure_classification(tmp_path: Path, failure: type[OSError]) -> None:
    with project_client(tmp_path) as (client, base):
        reference = upload(client, base, wav_bytes()).json()
        location = f"{base}/references/{reference['id']}"
        clip = client.post(f"{location}/clips", json={"start_ms": 0, "end_ms": 100}).json()
        before = client.get(f"{base}/references").json()
        with patch(
            "backend.app.audio.subprocess.run", side_effect=failure("private executable path")
        ):
            response = upload(client, base, wav_bytes())
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "audio_dependency_unavailable"
        assert "private executable path" not in response.text
        assert client.get(f"{base}/references").json() == before
        assert client.get(f"{location}/clips/{clip['id']}/audio").status_code == 200
        assert not list((tmp_path / "audio-tmp").glob("*"))
        assert len(list((tmp_path / "references").iterdir())) == 1


def test_database_commit_failure_cleans_new_files(tmp_path: Path) -> None:
    with project_client(tmp_path) as (client, base):
        reference = upload(client, base, wav_bytes()).json()
        location = f"{base}/references/{reference['id']}"
        clip = client.post(f"{location}/clips", json={"start_ms": 0, "end_ms": 100}).json()
        with connect(tmp_path / "projects.sqlite3") as connection:
            connection.execute(
                "CREATE TRIGGER fail_project_update BEFORE UPDATE ON projects "
                "BEGIN SELECT RAISE(ABORT, 'test commit failure'); END"
            )
        assert upload(client, base, wav_bytes()).status_code == 503
        assert (
            client.post(f"{location}/clips", json={"start_ms": 100, "end_ms": 300}).status_code
            == 503
        )
        listing = client.get(f"{base}/references").json()
        assert listing["active_clip_id"] == clip["id"]
        assert len(listing["references"]) == 1
        assert len(listing["references"][0]["clips"]) == 1
        assert len(list((tmp_path / "references").glob("*"))) == 1
        assert len(list((tmp_path / "references" / reference["id"] / "clips").glob("*"))) == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_clip_values_rejected(tmp_path: Path, value: str) -> None:
    with project_client(tmp_path) as (client, base):
        reference = upload(client, base, wav_bytes()).json()
        response = client.post(
            f"{base}/references/{reference['id']}/clips",
            content='{"start_ms": 0, "end_ms": ' + value + "}",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422
        assert client.get(f"{base}/references").json()["active_clip_id"] is None


@pytest.mark.parametrize("duration", [100, 600000])
def test_duration_inclusive_boundaries(tmp_path: Path, duration: int) -> None:
    with project_client(tmp_path) as (client, base):
        response = upload(client, base, wav_bytes(duration))
        assert response.status_code == 201, response.text
        assert response.json()["duration_ms"] == duration


def test_missing_files_return_storage_error(tmp_path: Path) -> None:
    with project_client(tmp_path) as (client, base):
        reference = upload(client, base, wav_bytes()).json()
        root = tmp_path / "references" / reference["id"]
        (root / "preview.wav").unlink()
        response = client.get(f"{base}/references/{reference['id']}/preview")
        assert response.status_code == 503
        assert str(tmp_path) not in response.text
        (root / "preview.wav").write_bytes(b"corrupt")
        response = client.post(
            f"{base}/references/{reference['id']}/clips", json={"start_ms": 0, "end_ms": 100}
        )
        assert response.status_code == 503
        assert not list((root / "clips").iterdir())


@pytest.mark.parametrize("failure", ["missing", "corrupt", "unreadable"])
def test_activation_file_failure_preserves_current_clip(tmp_path: Path, failure: str) -> None:
    with project_client(tmp_path) as (client, base):
        reference = upload(client, base, wav_bytes()).json()
        location = f"{base}/references/{reference['id']}"
        first = client.post(f"{location}/clips", json={"start_ms": 0, "end_ms": 100}).json()
        current = client.post(f"{location}/clips", json={"start_ms": 100, "end_ms": 300}).json()
        target = tmp_path / "references" / reference["id"] / "clips" / f"{first['id']}.wav"
        if failure == "missing":
            target.unlink()
        elif failure == "corrupt":
            target.write_bytes(b"corrupt")
        if failure == "unreadable":
            with patch(
                "backend.app.references.wave.open", side_effect=PermissionError("private path")
            ):
                response = client.put(f"{base}/active-clip", json={"clip_id": first["id"]})
        else:
            response = client.put(f"{base}/active-clip", json={"clip_id": first["id"]})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "storage_unavailable"
        assert str(tmp_path) not in response.text
        assert "private path" not in response.text
        listing = client.get(f"{base}/references").json()
        assert listing["active_clip_id"] == current["id"]
        assert client.get(f"{location}/clips/{current['id']}/audio").status_code == 200


def test_busy_limit_and_health_during_decode(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_decode(source: Path, destination: Path, suffix: str) -> int:
        started.set()
        assert release.wait(timeout=5)
        return decode(source, destination, suffix)

    with project_client(tmp_path) as (client, base), ThreadPoolExecutor(max_workers=1) as executor:
        with patch("backend.app.references.decode", side_effect=slow_decode):
            future = executor.submit(upload, client, base, wav_bytes())
            try:
                assert started.wait(timeout=5)
                assert client.get("/api/health").status_code == 200
                busy = upload(client, base, wav_bytes())
                assert busy.status_code == 429
                assert busy.json()["error"]["code"] == "audio_busy"
            finally:
                release.set()
            assert future.result(timeout=5).status_code == 201
        assert len(client.get(f"{base}/references").json()["references"]) == 1


def test_restart_cleans_interrupted_file_operations(tmp_path: Path) -> None:
    with project_client(tmp_path) as (client, base):
        reference = upload(client, base, wav_bytes()).json()
    stage = tmp_path / "audio-tmp" / str(uuid4())
    stage.mkdir(parents=True)
    (stage / "original").write_bytes(b"partial")
    orphan = tmp_path / "references" / str(uuid4())
    orphan.mkdir()
    (orphan / "original").write_bytes(b"uncommitted")
    clips = tmp_path / "references" / reference["id"] / "clips"
    clips.mkdir()
    (clips / f"{uuid4()}.wav").write_bytes(b"incomplete")
    with TestClient(create_app(tmp_path)) as client:
        assert len(client.get(f"{base}/references").json()["references"]) == 1
        assert client.get(f"{base}/references/{reference['id']}/original").content == wav_bytes()
        assert not stage.exists()
        assert not orphan.exists()
        assert not list(clips.iterdir())


def test_second_service_cannot_cleanup_active_directory(tmp_path: Path) -> None:
    with project_client(tmp_path) as (first, base):
        pending = tmp_path / "audio-tmp" / "in-progress"
        pending.mkdir(parents=True)
        (pending / "original").write_bytes(b"upload in progress")
        with TestClient(create_app(tmp_path)) as second:
            assert second.get("/api/health").status_code == 503
            assert second.get(f"{base}/references").status_code == 503
            assert (pending / "original").read_bytes() == b"upload in progress"
            assert first.get("/api/health").status_code == 200
        assert first.get("/api/health").status_code == 200
    with TestClient(create_app(tmp_path)) as third:
        assert third.get("/api/health").status_code == 200
        assert not pending.exists()


def test_m01_database_upgrades_without_data_loss(tmp_path: Path) -> None:
    old = tmp_path / "old-migrations"
    old.mkdir()
    first = Path(__file__).parents[1] / "app/migrations/001_projects.sql"
    (old / first.name).write_bytes(first.read_bytes())
    database = tmp_path / "projects.sqlite3"
    migrate(database, old)
    with connect(database) as connection:
        connection.execute("INSERT INTO projects VALUES ('existing', '项目', 'now', 'now')")
    migrate(database)
    migrate(database)
    with connect(database) as connection:
        assert (
            connection.execute("SELECT name FROM projects WHERE id='existing'").fetchone()[0]
            == "项目"
        )
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3


def test_actual_restart_preserves_audio_and_active_clip(tmp_path: Path) -> None:
    with server(tmp_path) as url:
        project = httpx.post(f"{url}/api/projects", json={"name": "音频重启"}).json()
        base = f"{url}/api/projects/{project['id']}"
        response = httpx.post(
            f"{base}/references?filename=test.wav",
            content=wav_bytes(),
            headers={"content-type": "application/octet-stream"},
        )
        assert response.status_code == 201, response.text
        reference = response.json()
        clip = httpx.post(
            f"{base}/references/{reference['id']}/clips", json={"start_ms": 0, "end_ms": 100}
        ).json()
    with server(tmp_path) as url:
        base = f"{url}/api/projects/{project['id']}"
        listing = httpx.get(f"{base}/references").json()
        assert listing["active_clip_id"] == clip["id"]
        assert listing["references"][0]["clips"] == [clip]
        assert httpx.get(f"{base}/references/{reference['id']}/original").content == wav_bytes()
        audio = httpx.get(f"{base}/references/{reference['id']}/clips/{clip['id']}/audio")
        with wave.open(io.BytesIO(audio.content), "rb") as decoded:
            assert decoded.getnframes() == 2400
