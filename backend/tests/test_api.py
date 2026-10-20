import os
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.storage import MigrationError, connect, migrate


def test_projects(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/api/projects").json() == {"projects": []}
        response = client.post("/api/projects", json={"name": "  武汉项目  "})
        assert response.status_code == 201
        project = response.json()
        assert project["name"] == "武汉项目"
        assert project["created_at"].endswith("Z")
        assert project["created_at"] == project["updated_at"]
        assert client.post("/api/projects", json={"name": "武汉项目"}).status_code == 201
        project_url = f"/api/projects/{project['id']}"
        assert client.get(project_url).json() == project
        renamed = client.patch(project_url, json={"name": "新名称"}).json()
        assert renamed["name"] == "新名称"
        assert renamed["created_at"] == project["created_at"]
        assert renamed["updated_at"] >= project["updated_at"]
        projects = client.get("/api/projects").json()["projects"]
        assert len(projects) == 2
        assert projects[0]["id"] == project["id"]
        assert client.get(f"/api/projects/{uuid4()}").status_code == 404
        assert client.patch(f"/api/projects/{uuid4()}", json={"name": "x"}).status_code == 404
        for endpoint in ("/api", "/api/unknown", "/api/projects/bad-uuid"):
            response = client.get(endpoint)
            assert response.status_code in (404, 422)
            assert response.headers["content-type"].startswith("application/json")
            assert "error" in response.json()


@pytest.mark.parametrize("name", ["", " ", "x" * 81, "x\ny", "\tx", "x\x00", "x\x7f", 12, None])
def test_invalid_names(tmp_path: Path, name: object) -> None:
    with TestClient(create_app(tmp_path)) as client:
        project = client.post("/api/projects", json={"name": "valid"}).json()
        for method, url in (
            (client.post, "/api/projects"),
            (client.patch, f"/api/projects/{project['id']}"),
        ):
            response = method(url, json={"name": name})
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "invalid_input"
        assert client.get(f"/api/projects/{project['id']}").json()["name"] == "valid"


def test_boundary_and_invalid_payload(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        for name in ("字", "字" * 80, "  字  "):
            assert client.post("/api/projects", json={"name": name}).status_code == 201
        payloads: tuple[object, ...] = ({}, {"name": "x", "other": True}, [], "x")
        for payload in payloads:
            assert client.post("/api/projects", json=payload).status_code == 422
        assert client.post("/api/projects", content="{bad").status_code == 422


def test_storage_failure_is_safe(tmp_path: Path) -> None:
    occupied = tmp_path / "private-path"
    occupied.write_text("not a directory")
    with TestClient(create_app(occupied)) as client:
        for response in (
            client.get("/api/health"),
            client.get("/api/projects"),
            client.post("/api/projects", json={"name": "x"}),
        ):
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "storage_unavailable"
            assert str(tmp_path) not in response.text


def test_runtime_storage_failure(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        (tmp_path / "projects.sqlite3").unlink()
        (tmp_path / "projects.sqlite3").mkdir()
        assert client.get("/api/projects").status_code == 503


def test_migrations_idempotent_and_tampering(tmp_path: Path) -> None:
    database = tmp_path / "projects.sqlite3"
    migrate(database)
    with connect(database) as connection:
        connection.execute("INSERT INTO projects VALUES ('id', 'keep', 'now', 'now')")
    migrate(database)
    with connect(database) as connection:
        assert connection.execute("SELECT name FROM projects").fetchone()[0] == "keep"
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 11
        connection.execute("UPDATE schema_migrations SET checksum = 'changed'")
    with pytest.raises(MigrationError, match="变化"):
        migrate(database)
    with pytest.raises(MigrationError, match="变化"), TestClient(create_app(tmp_path)):
        pass


def test_future_migration_refused(tmp_path: Path) -> None:
    database = tmp_path / "projects.sqlite3"
    migrate(database)
    with connect(database) as connection:
        connection.execute("INSERT INTO schema_migrations VALUES (999, 'future')")
    with pytest.raises(MigrationError, match="高于"):
        migrate(database)
    with pytest.raises(MigrationError, match="高于"), TestClient(create_app(tmp_path)):
        pass


def test_migration_failure_rolls_back(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "001_bad.sql").write_text("CREATE TABLE partial (id TEXT);\nINVALID SQL;\n")
    database = tmp_path / "db.sqlite3"
    with pytest.raises(sqlite3.Error):
        migrate(database, directory)
    with connect(database) as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        )


@contextmanager
def server(data_dir: Path) -> Iterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = os.environ.copy()
    env["VOICE_DATA_DIR"] = str(data_dir)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("uvicorn exited before readiness")
            try:
                if httpx.get(f"{url}/api/health", timeout=0.5).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("uvicorn did not become ready")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_actual_process_restart_preserves_project(tmp_path: Path) -> None:
    with server(tmp_path) as url:
        response = httpx.post(f"{url}/api/projects", json={"name": "重启保留"})
        assert response.status_code == 201
        project = response.json()
    with server(tmp_path) as url:
        assert httpx.get(f"{url}/api/projects/{project['id']}").json() == project
        assert httpx.get(f"{url}/api/projects").json() == {"projects": [project]}
