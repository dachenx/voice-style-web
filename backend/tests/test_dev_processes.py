"""Exercise the development supervisor's cleanup with real process groups."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.dev import signal_group, stop_processes


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups; documented macOS/Linux runner")
@pytest.mark.parametrize("leader_exits", [False, True])
def test_child_port_released_after_leader_exit(tmp_path: Path, leader_exits: bool) -> None:
    ready = tmp_path / "port.txt"
    child_code = (
        "import socket, time, signal; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(); "
        f"Path({str(ready)!r}).write_text(str(s.getsockname()[1])); time.sleep(60)"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        + ("time.sleep(0.1)" if leader_exits else "time.sleep(60)")
    )
    process = subprocess.Popen([sys.executable, "-c", parent_code], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < deadline, "child did not bind"
            time.sleep(0.02)
        port = int(ready.read_text())
        if leader_exits:
            assert process.wait(timeout=5) == 0
        stop_processes([process])
        # Includes an already-gone group: cleanup must remain safe and idempotent.
        stop_processes([process])
        while True:
            try:
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", port))
                break
            except OSError:
                assert time.monotonic() < deadline, "orphan child kept the port open"
                time.sleep(0.02)
    finally:
        signal_group(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


@pytest.mark.parametrize("states", ["123 Z\n456 S\n", "456 S\n"])
def test_already_exited_group_permission_error_is_safe(
    monkeypatch: pytest.MonkeyPatch, states: str
) -> None:
    def denied(group: int, sig: int) -> None:
        raise PermissionError("group has exited")

    monkeypatch.setattr(os, "killpg", denied)
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, states)
    )
    signal_group(123, signal.SIGTERM)


def test_live_group_permission_failure_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(group: int, sig: int) -> None:
        raise PermissionError("live group cannot be signalled")

    monkeypatch.setattr(os, "killpg", denied)
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "123 S\n")
    )
    with pytest.raises(PermissionError, match="live group"):
        signal_group(123, signal.SIGTERM)


@pytest.mark.parametrize("inspection", ["fails", "malformed"])
def test_unverified_group_keeps_permission_error(
    monkeypatch: pytest.MonkeyPatch, inspection: str
) -> None:
    def denied(group: int, sig: int) -> None:
        raise PermissionError("cannot verify group")

    def inspect(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if inspection == "fails":
            raise subprocess.CalledProcessError(1, "ps")
        return subprocess.CompletedProcess("ps", 0, "unexpected output with extra columns")

    monkeypatch.setattr(os, "killpg", denied)
    monkeypatch.setattr(subprocess, "run", inspect)
    with pytest.raises(PermissionError, match="cannot verify group"):
        signal_group(123, signal.SIGTERM)
