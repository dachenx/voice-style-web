"""Single local generation worker, protected separately from the API service."""

import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import sqlite3
import sys
import threading
import time
import wave
from pathlib import Path
from types import FrameType
from typing import Any
from uuid import uuid4

from .directory_lock import DirectoryLock
from .generation import PARAGRAPH_OUTPUT_CONTRACT, GenerationRegistry, TaskError
from .local_model import LocalCosyVoiceAdapter
from .local_qwen import LocalQwenAdapter
from .references import timestamp
from .segment_outputs import collect_segments
from .storage import connect, pending_deletion_ids
from .tasks import canonical, file_digest, task_from_row, validate_clip

logger = logging.getLogger(__name__)


class WorkerSession:
    """Own the worker lock across recovery, claiming, generation and publication."""

    def __init__(
        self,
        directory: Path,
        registry: GenerationRegistry | None = None,
        model_config_path: Path | None = None,
    ) -> None:
        self.directory = directory
        self.database = directory / "projects.sqlite3"
        self.registry = (
            registry
            if registry is not None
            else GenerationRegistry(
                model_config_path=model_config_path, runtime_directory=directory / "model-health"
            )
        )
        self.owner = str(uuid4())
        self.lock = DirectoryLock(directory, ".worker.lock")
        self.stop = threading.Event()
        self.heartbeat_stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = "idle"
        self.active = False

    def __enter__(self) -> "WorkerSession":
        self.lock.acquire()
        try:
            self.active = True
            self.recover()
            self.heartbeat()
            self.thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
            self.thread.start()
        except BaseException:
            self.active = False
            self.lock.close()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop.set()
        self.heartbeat_stop.set()
        if self.thread is not None:
            self.thread.join()
        try:
            self.state = "stopped"
            self.heartbeat()
        finally:
            self.active = False
            self.lock.close()

    def heartbeat(self) -> None:
        with connect(self.database) as connection:
            connection.execute(
                "INSERT INTO generation_worker_health VALUES (1,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET owner_id=excluded.owner_id,"
                "state=excluded.state,last_seen_at=excluded.last_seen_at",
                (self.owner, self.state, timestamp()),
            )

    def heartbeat_loop(self) -> None:
        while not self.heartbeat_stop.wait(1):
            try:
                self.heartbeat()
            except (OSError, sqlite3.Error):
                logger.exception("工作进程心跳写入失败")
                self.stop.set()
                return

    def recover(self) -> None:
        cleanup_lock = DirectoryLock(self.directory, ".project-cleanup.lock")
        while True:
            try:
                cleanup_lock.acquire()
                break
            except BlockingIOError:
                if self.stop.wait(0.05):
                    return
        try:
            self._recover_locked()
        finally:
            cleanup_lock.close()

    def _recover_locked(self) -> None:
        # Caller already owns .worker.lock: a live older worker can never be recovered here.
        with connect(self.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            succeeded = {
                row[0]
                for row in connection.execute(
                    "SELECT id FROM generation_tasks WHERE state='succeeded'"
                )
            }
        temporary = self.directory / "generation-tmp"
        pending_tmp = pending_deletion_ids(self.database, "generation-tmp")
        if temporary.exists():
            for item in temporary.iterdir():
                if item.name not in pending_tmp:
                    if item.is_dir() and not item.is_symlink():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
        root = self.directory / "generation-results"
        pending_results = pending_deletion_ids(self.database, "generation-results")
        if root.exists():
            for item in root.iterdir():
                if item.name not in succeeded and item.name not in pending_results:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()

        with connect(self.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = timestamp()
            connection.execute(
                "UPDATE generation_tasks SET state='failed',phase='failed',"
                "error_code='worker_interrupted',error_message='上次运行中断，请显式重试；原输入已保留',"
                "updated_at=?,finished_at=?,state_version=state_version+1 WHERE state='running'",
                (now, now),
            )

    def claim(self) -> dict[str, Any] | None:
        if not self.active:
            raise RuntimeError("工作进程必须先取得互斥锁")
        if self.stop.is_set():
            return None
        with connect(self.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM generation_tasks WHERE state='queued' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = timestamp()
            connection.execute(
                "UPDATE generation_tasks SET state='running',phase='validating',"
                "started_at=?,updated_at=?,state_version=state_version+1 "
                "WHERE id=? AND state='queued'",
                (now, now, row["id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM generation_tasks WHERE id=?", (row["id"],)
            ).fetchone()
            assert claimed is not None
            return task_from_row(claimed)

    def progress(self, task_id: str, completed: int) -> None:
        if self.stop.is_set():
            raise TaskError(503, "worker_interrupted", "工作进程停止，任务中断，请显式重试")
        with connect(self.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM generation_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None or row["state"] != "running":
                raise TaskError(409, "task_state_changed", "任务运行状态已改变")
            if (
                type(completed) is not int
                or completed < row["completed_paragraphs"]
                or completed > row["total_paragraphs"]
            ):
                raise TaskError(502, "adapter_invalid_output", "模拟适配器返回无效进度")
            connection.execute(
                "UPDATE generation_tasks SET completed_paragraphs=?,phase='generating',"
                "updated_at=?,state_version=state_version+1 WHERE id=? AND state='running'",
                (completed, timestamp(), task_id),
            )

    def fail(self, task_id: str, code: str, message: str) -> None:
        with connect(self.database) as connection:
            now = timestamp()
            connection.execute(
                "UPDATE generation_tasks SET state='failed',phase='failed',"
                "error_code=?,error_message=?,updated_at=?,finished_at=?,"
                "state_version=state_version+1 WHERE id=? AND state='running'",
                (code, message, now, now, task_id),
            )

    def run_once(self) -> bool:
        task = self.claim()
        if task is None:
            return False
        self.state = "running"
        self.heartbeat()
        task_id = task["id"]
        stage = self.directory / "generation-tmp" / task_id
        target = self.directory / "generation-results" / task_id
        started = time.monotonic()
        published = False
        failure: tuple[str, str] | None = None
        try:
            stage.mkdir(parents=True)
            snapshot = task["snapshot"]
            if snapshot.get("output_contract") not in (None, PARAGRAPH_OUTPUT_CONTRACT):
                raise TaskError(
                    409, "output_contract_unsupported", "任务输出协议不受支持，原输入保留"
                )
            reference = snapshot["reference"]
            clip = reference["clip"]
            adapter = self.registry.for_snapshot(snapshot)
            if snapshot.get("reference_source") == "persona":
                if not isinstance(adapter, LocalQwenAdapter) or reference != adapter.reference():
                    raise TaskError(409, "persona_changed", "固定人物参考不匹配")
                path = Path(adapter.require_configuration()["reference_path"])
            else:
                if isinstance(adapter, LocalQwenAdapter):
                    raise TaskError(409, "persona_source_missing", "人物任务来源缺失")
                path = (
                    self.directory / "references" / reference["id"] / "clips" / f"{clip['id']}.wav"
                )
            actual = validate_clip(path, clip["start_ms"], clip["end_ms"])
            if actual != reference["clip_sha256"]:
                raise TaskError(409, "reference_changed", "任务参考文件已改变，未使用修改后的素材")
            if adapter.capability.model_dump(mode="json") != snapshot["adapter"]:
                raise TaskError(409, "adapter_version_changed", "任务适配器版本已改变，原输入保留")
            self.progress(task_id, 0)
            destination = stage / "audio.wav"
            if isinstance(adapter, (LocalCosyVoiceAdapter, LocalQwenAdapter)):
                assert self.lock.file is not None
                adapter.bind_execution(
                    path,
                    self.lock.file.fileno(),
                    task_id,
                    self.stop.is_set,
                    self.directory / "model-health",
                )
            adapter.generate(
                json.loads(canonical(snapshot)),
                destination,
                lambda completed: self.progress(task_id, completed),
            )
            with connect(self.database) as connection:
                row = connection.execute(
                    "SELECT completed_paragraphs FROM generation_tasks WHERE id=?", (task_id,)
                ).fetchone()
                if row is None or row[0] != task["total_paragraphs"]:
                    raise TaskError(502, "adapter_invalid_output", "模拟适配器未完成全部段落")
                connection.execute(
                    "UPDATE generation_tasks SET phase='finalizing',updated_at=?,"
                    "state_version=state_version+1 WHERE id=? AND state='running'",
                    (timestamp(), task_id),
                )
            result = self.inspect_result(destination, snapshot, started, task_id)
            if isinstance(adapter, (LocalCosyVoiceAdapter, LocalQwenAdapter)):
                result.update(adapter.last_result_metadata)
            if snapshot.get("output_contract") == PARAGRAPH_OUTPUT_CONTRACT:
                result["segments"] = collect_segments(stage, snapshot)
            if self.stop.is_set():
                raise TaskError(503, "worker_interrupted", "工作进程停止，任务中断，请显式重试")
            target.parent.mkdir(exist_ok=True)
            stage.rename(target)
            with connect(self.database) as connection:
                connection.execute("BEGIN IMMEDIATE")
                now = timestamp()
                changed = connection.execute(
                    "UPDATE generation_tasks SET state='succeeded',phase='succeeded',"
                    "result_json=?,updated_at=?,finished_at=?,state_version=state_version+1 "
                    "WHERE id=? AND state='running'",
                    (canonical(result), now, now, task_id),
                )
                if changed.rowcount != 1:
                    raise TaskError(409, "task_state_changed", "任务运行状态已改变")
            published = True
        except Exception as exc:
            logger.exception("生成任务失败")
            if isinstance(exc, TaskError):
                code, message = exc.code, exc.message
                if code == "model_cleanup_failed":
                    self.stop.set()
            elif isinstance(exc, (OSError, sqlite3.Error)):
                code, message = (
                    "storage_unavailable",
                    "任务存储暂不可用，原输入已保留，请检查后重试",
                )
            else:
                code, message = "generation_failed", "生成失败，原输入已保留，请重试"
            failure = (code, message)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            if not published and target.exists():
                shutil.rmtree(target)
            if failure is not None:
                self.fail(task_id, *failure)
            self.state = "idle"
            self.heartbeat()
        return True

    @staticmethod
    def inspect_result(
        path: Path, snapshot: dict[str, Any], started: float, task_id: str
    ) -> dict[str, Any]:
        try:
            with wave.open(str(path), "rb") as audio:
                rate, channels, width, frames = (
                    audio.getframerate(),
                    audio.getnchannels(),
                    audio.getsampwidth(),
                    audio.getnframes(),
                )
                if (
                    rate != 24000
                    or channels != 1
                    or width != 2
                    or not 1 <= frames <= rate * 600
                    or len(audio.readframes(frames)) != frames * width * channels
                ):
                    raise TaskError(
                        502, "adapter_invalid_output", "生成结果不是完整的 24k 单声道 PCM WAV"
                    )
        except (wave.Error, EOFError) as exc:
            raise TaskError(502, "adapter_invalid_output", "生成结果 WAV 无效") from exc
        prefix = "simulated" if snapshot.get("is_simulated", True) else "generated"
        return {
            "filename": f"{prefix}-{task_id}.wav",
            "media_type": "audio/wav",
            "byte_size": path.stat().st_size,
            "sha256": file_digest(path),
            "duration_ms": frames * 1000 // rate,
            "sample_rate": rate,
            "channels": channels,
            "sample_width": width,
            "frame_count": frames,
            "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
            "input_sha256": hashlib.sha256(canonical(snapshot).encode("utf-8")).hexdigest(),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--watch-parent", action="store_true")
    parser.add_argument("--model-config", type=Path)
    args = parser.parse_args()
    session = WorkerSession(args.data_dir, model_config_path=args.model_config)

    def stop(signum: int, frame: FrameType | None) -> None:
        session.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if args.watch_parent:

        def parent_closed() -> None:
            # A daemon must not hold BufferedReader's lock during SIGTERM shutdown.
            try:
                os.read(sys.stdin.fileno(), 1)
            finally:
                session.stop.set()

        threading.Thread(target=parent_closed, daemon=True).start()
    try:
        with session:
            while not session.stop.is_set():
                if not session.run_once():
                    session.stop.wait(0.2)
    except (OSError, sqlite3.Error):
        logger.exception("工作进程无法启动或存储不可用")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
