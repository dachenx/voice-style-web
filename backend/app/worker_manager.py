"""API-owned subprocess lifecycle; worker lock belongs exclusively to its child."""

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from .tasks import worker_health

logger = logging.getLogger(__name__)


class WorkerManager:
    def __init__(self, directory: Path, enabled: bool) -> None:
        self.directory = directory
        self.enabled = enabled
        self.process: subprocess.Popen[bytes] | None = None
        self.failed = False

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "backend.app.worker",
                    "--data-dir",
                    str(self.directory.resolve()),
                    "--watch-parent",
                ],
                cwd=Path(__file__).resolve().parents[2],
                stdin=subprocess.PIPE,
            )
        except OSError:
            logger.exception("无法启动独立工作进程")
            self.failed = True

    def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"state": "stopped", "last_seen_at": None, "error": None}
        if self.failed or self.process is None or self.process.poll() is not None:
            return {
                "state": "unavailable",
                "last_seen_at": None,
                "error": {
                    "code": "worker_unavailable",
                    "message": "独立工作进程不可用，任务已保留，请重启服务",
                },
            }
        health = worker_health(self.directory / "projects.sqlite3")
        if health["state"] == "stopped":
            return {"state": "starting", "last_seen_at": health["last_seen_at"], "error": None}
        return health

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
