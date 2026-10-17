"""Startup readiness and failure recovery without using production data."""
import io
import os
import socket
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from scripts import dev


def environment(tmp_path, monkeypatch, built=False):
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    for name in (".venv/bin/python", "frontend/dist/index.html",
                 "frontend/node_modules/vite/bin/vite.js"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(dev.subprocess, "run", Mock())
    monkeypatch.setattr(dev, "validate_node", Mock())
    return tmp_path


def test_development_uses_registered_environment(tmp_path, monkeypatch):
    environment(tmp_path, monkeypatch)
    expected = dict(os.environ, STARTUP_TEST="yes")
    monkeypatch.setattr(dev, "configured_environment", lambda root: expected)
    monkeypatch.setattr(dev.socket, "socket", MagicMock())
    python, env = dev.preflight(False)
    assert python == tmp_path / ".venv/bin/python"
    assert env is expected
    dev.validate_node.assert_called_once_with(expected)


def test_built_ignores_invalid_node_config(tmp_path, monkeypatch):
    environment(tmp_path, monkeypatch)
    monkeypatch.setattr(dev, "configured_environment", Mock(side_effect=AssertionError))
    monkeypatch.setattr(dev.socket, "socket", MagicMock())
    dev.preflight(True)
    dev.validate_node.assert_not_called()


@pytest.mark.parametrize("missing, message, built", [
    (".venv/bin/python", "缺少 .venv", False),
    ("frontend/dist/index.html", "缺少页面构建", True),
    ("frontend/node_modules/vite/bin/vite.js", "缺少前端依赖", False),
])
def test_missing_dependencies_explain_recovery(tmp_path, monkeypatch, missing, message, built):
    environment(tmp_path, monkeypatch)
    (tmp_path / missing).unlink()
    with pytest.raises(ValueError, match=message):
        dev.preflight(built)


def test_bad_backend_and_node_config(tmp_path, monkeypatch):
    environment(tmp_path, monkeypatch)
    dev.subprocess.run.side_effect = subprocess.CalledProcessError(1, "python")
    with pytest.raises(ValueError, match="后端环境不可用"):
        dev.preflight(False)
    dev.subprocess.run.side_effect = None
    config = tmp_path / ".quality-local/runtime.json"
    config.parent.mkdir()
    config.write_text("{bad")
    with pytest.raises(ValueError, match="本地运行时配置无法读取"):
        dev.preflight(False)


def test_occupied_port_is_not_stopped(tmp_path, monkeypatch):
    environment(tmp_path, monkeypatch)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        real_socket = socket.socket
        def listener():
            instance = Mock(wraps=real_socket())
            instance.__enter__ = Mock(return_value=instance)
            instance.__exit__ = Mock(side_effect=lambda *args: instance.close())
            instance.bind = lambda address: instance._mock_wraps.bind(occupied.getsockname())
            return instance
        monkeypatch.setattr(dev.socket, "socket", listener)
        with pytest.raises(ValueError, match="端口 8000"):
            dev.preflight(True)
        assert occupied.getsockname()[1] > 0


class Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "text/html"}


def test_readiness_waits_for_health_page_and_proxy(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    responses = [OSError("503"), Response(b'{"status":"ok"}'),
                 Response(b"html"), Response(b'{"status":"bad"}'),
                 Response(b'{"status":"ok"}'), Response(b"html"),
                 Response(b'{"status":"ok"}')]
    opener = Mock()
    opener.open.side_effect = responses
    monkeypatch.setattr(dev.urllib.request, "build_opener", lambda *args: opener)
    monkeypatch.setattr(dev.time, "sleep", Mock())
    dev.wait_ready([process], "http://127.0.0.1:5173")
    assert opener.open.call_count == 7
    assert opener.open.call_args.args[0].endswith("5173/api/health")


def test_timeout_and_child_exit(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    with pytest.raises(ValueError, match="超过30秒"):
        dev.wait_ready([process], "http://127.0.0.1:5173", timeout=0)
    process.poll.return_value = 1
    with pytest.raises(ValueError, match="启动期间退出"):
        dev.wait_ready([process], "http://127.0.0.1:5173")


@pytest.mark.parametrize("failure", ["spawn", "readiness", "interrupt", "running", "browser_error"])
def test_main_cleans_partial_start_and_never_opens_early(monkeypatch, capsys, failure):
    expected_env = {"PATH": "registered"}
    monkeypatch.setattr(dev, "preflight", lambda built: (Path("python"), expected_env))
    process = Mock()
    process.poll.return_value = 1
    if failure == "browser_error":
        process.poll.side_effect = [None, None, 1]
    sleep = Mock()
    monkeypatch.setattr(dev.time, "sleep", sleep)
    spawn = Mock(side_effect=[process, OSError("npm unavailable")] if failure == "spawn"
                 else [process, process])
    monkeypatch.setattr(dev.subprocess, "Popen", spawn)
    ready = Mock(side_effect=ValueError("not ready") if failure == "readiness"
                 else KeyboardInterrupt if failure == "interrupt" else None)
    monkeypatch.setattr(dev, "wait_ready", ready)
    cleanup = Mock()
    monkeypatch.setattr(dev, "stop_processes", cleanup)
    browser = Mock(return_value=False, side_effect=OSError("opener failed")
                   if failure == "browser_error" else None)
    monkeypatch.setattr(dev.webbrowser, "open", browser)
    assert dev.main(["--open"]) == (0 if failure == "interrupt" else 1)
    cleanup.assert_called_once_with([process] if failure == "spawn" else [process, process])
    assert spawn.call_args.kwargs["env"] is expected_env
    output = capsys.readouterr()
    if failure not in {"running", "browser_error"}:
        browser.assert_not_called()
        assert "服务已就绪" not in output.out
    else:
        browser.assert_called_once()
        assert "手动打开" in output.out
        assert "意外退出" in output.err
        if failure == "browser_error":
            sleep.assert_called_once_with(0.3)


@pytest.mark.skipif(os.name == "nt", reason="POSIX address reuse")
def test_preflight_accepts_time_wait_but_rejects_live_listener(tmp_path, monkeypatch):
    environment(tmp_path, monkeypatch)
    real_socket = socket.socket
    with real_socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        address = listener.getsockname()
        client = socket.create_connection(address)
        connection, _ = listener.accept()
        connection.close()
        assert client.recv(1) == b""
        client.close()

        class Probe:
            def __init__(self):
                self.socket = real_socket()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.socket.close()
            def setsockopt(self, *args):
                self.socket.setsockopt(*args)
            def bind(self, ignored):
                self.socket.bind(address)

        monkeypatch.setattr(dev.socket, "socket", Probe)
        with pytest.raises(ValueError, match="已被占用"):
            dev.preflight(True)
    # The server closed first, leaving TIME_WAIT; actual POSIX servers reuse it.
    dev.preflight(True)
