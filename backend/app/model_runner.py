"""Supervise an offline model subprocess without importing its dependencies."""

import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .audio import decode
from .generation import PARAGRAPH_OUTPUT_CONTRACT, TaskError
from .model_config import canonical_config, digest_file, digest_json

logger = logging.getLogger(__name__)
ERROR_MESSAGES = {
    "model_configuration_changed": "固定模型配置已改变，请核对本机环境",
    "model_dependency_changed": "固定模型的关键依赖版本已改变，未使用新环境替换原任务",
    "reference_changed": "固定参考音频内容已改变",
    "transcript_required": "固定参考片段缺少匹配的准确原话",
    "text_limit_exceeded": "当前段落超过本适配器操作限额，请缩短段落并重新确认",
    "output_limit_exceeded": "模型输出超过本适配器资源限额",
    "invalid_model_output": "真实模型未返回完整有效音频",
    "model_inference_failed": "本机模型加载或推理失败，请查看本机诊断日志",
}


class ControlledModelRunner:
    bridge_script: Path
    configuration: dict[str, Any] | None
    reference_path: Path | None
    lock_fd: int | None
    task_id: str | None
    should_stop: Callable[[], bool]
    runtime_directory: Path | None
    bridge_command: tuple[str, ...] | None
    bridge_timeout: float | None
    last_result_metadata: dict[str, Any]

    def require_configuration(self) -> dict[str, Any]:
        raise NotImplementedError

    def bind_execution(
        self,
        reference: Path,
        lock_fd: int,
        task_id: str,
        should_stop: Callable[[], bool],
        runtime_directory: Path,
    ) -> None:
        self.reference_path, self.lock_fd, self.task_id = reference, lock_fd, task_id
        self.should_stop, self.runtime_directory = should_stop, runtime_directory

    @staticmethod
    def stop_process(process: subprocess.Popen[bytes]) -> None:
        # The leader can already be dead while its children still execute in our group.
        def live_members() -> bool:
            states = subprocess.run(
                ["ps", "-eo", "pgid=,stat="],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
            return any(
                int(group) == process.pid and not state.startswith("Z")
                for group, state in (line.split() for line in states.splitlines())
            )

        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if live_members():
                    raise
            process.wait(timeout=5)
            deadline = time.monotonic() + 5
            while live_members():
                if time.monotonic() > deadline:
                    raise TimeoutError("Owned model process group remains live")
                time.sleep(0.02)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise TaskError(
                503,
                "model_cleanup_failed",
                "模型进程回收未能确认，队列已停止，请检查本机进程后重启",
            ) from exc
        finally:
            if process.stdin is not None:
                process.stdin.close()

    def generate(
        self, snapshot: dict[str, Any], destination: Path, progress: Callable[[int], None]
    ) -> None:
        config = self.require_configuration()
        if (
            os.name != "posix"
            or self.reference_path is None
            or self.lock_fd is None
            or self.task_id is None
        ):
            raise TaskError(
                503, "model_execution_unavailable", "真实模型必须在受控且持锁的工作进程中运行"
            )
        work = destination.parent / "bridge-work"
        work.mkdir()
        process: subprocess.Popen[bytes] | None = None
        try:
            reference = work / "reference.wav"
            shutil.copyfile(self.reference_path, reference)
            if digest_file(reference) != snapshot["reference"]["clip_sha256"]:
                raise TaskError(409, "reference_changed", "固定参考音频复制核验失败")
            request = work / "request.json"
            request.write_text(
                canonical_config(
                    {
                        "schema_version": 1,
                        "configuration": config,
                        "snapshot": snapshot,
                        "task_id": self.task_id,
                        "reference_path": str(reference.resolve()),
                    }
                ),
                encoding="utf-8",
            )
            assert self.runtime_directory is not None
            self.runtime_directory.mkdir(exist_ok=True)
            status_path = self.runtime_directory / f"{config['config_fingerprint']}.json"
            progress_path, native = work / "progress.json", work / "native.wav"
            command = (
                list(self.bridge_command)
                if self.bridge_command is not None
                else [config["python_executable"], str(self.bridge_script)]
            )
            command += [
                "--input",
                str(request.resolve()),
                "--output",
                str(native.resolve()),
                "--progress",
                str(progress_path.resolve()),
                "--status",
                str(status_path.resolve()),
            ]
            env = {
                **os.environ,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "MODELSCOPE_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "CUDA_VISIBLE_DEVICES": "",
            }
            started = time.monotonic()
            timeout = (
                self.bridge_timeout
                if self.bridge_timeout is not None
                else config["limits"]["task_timeout_seconds"]
            )
            completed = 0
            paragraph_index = 0
            paragraph_started = started
            with (work / "diagnostic.log").open("wb") as diagnostic:
                process = subprocess.Popen(
                    command,
                    env=env,
                    cwd=destination.parent,
                    stdin=subprocess.PIPE,
                    stdout=diagnostic,
                    stderr=diagnostic,
                    start_new_session=True,
                    pass_fds=(self.lock_fd,),
                )
                while True:
                    if self.should_stop():
                        raise TaskError(
                            503, "worker_interrupted", "工作进程停止，真实推理已回收，请显式重试"
                        )
                    now = time.monotonic()
                    if now - started > timeout:
                        raise TaskError(504, "model_timeout", "本机模型推理超时，子进程已回收")
                    if progress_path.exists():
                        value = json.loads(progress_path.read_text(encoding="utf-8"))[
                            "completed_paragraphs"
                        ]
                        if type(value) is not int or not completed <= value <= len(
                            snapshot["paragraph_ids"]
                        ):
                            raise TaskError(502, "adapter_invalid_output", "模型执行桥返回无效进度")
                        if value > completed:
                            progress(value)
                            completed = value
                    if status_path.exists():
                        status = json.loads(status_path.read_text(encoding="utf-8"))
                        index = (
                            status.get("paragraph_index", 0)
                            if status.get("task_id") == self.task_id
                            and status.get("pid") == process.pid
                            else 0
                        )
                        if index != paragraph_index:
                            paragraph_index, paragraph_started = index, now
                    limit = (
                        config["limits"]["paragraph_timeout_seconds"]
                        if paragraph_index
                        else config["limits"]["model_load_timeout_seconds"]
                    )
                    if now - paragraph_started > limit:
                        raise TaskError(504, "model_timeout", "本机模型阶段超时，子进程已回收")
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                process.wait(timeout=5)
            if process.returncode != 0:
                try:
                    code = json.loads(
                        native.with_suffix(".error.json").read_text(encoding="utf-8")
                    )["code"]
                except (OSError, ValueError, KeyError):
                    code = "model_inference_failed"
                if code not in ERROR_MESSAGES:
                    code = "model_inference_failed"
                logger.error(
                    "模型执行桥诊断: %s",
                    (work / "diagnostic.log").read_text(errors="replace")[-4096:],
                )
                raise TaskError(502, code, ERROR_MESSAGES[code])
            metadata = json.loads(native.with_suffix(".meta.json").read_text(encoding="utf-8"))
            expected = {
                "input_sha256": hashlib.sha256(canonical_config(snapshot).encode()).hexdigest(),
                "reference_sha256": snapshot["reference"]["clip_sha256"],
                "config_fingerprint": config["config_fingerprint"],
                "model_fingerprint": config["model_fingerprint"],
                "environment_fingerprint": digest_json(config["environment"]),
            }
            if any(metadata.get(key) != value for key, value in expected.items()):
                raise TaskError(502, "adapter_invalid_output", "模型结果与固定输入身份不匹配")
            if snapshot.get("output_contract") == PARAGRAPH_OUTPUT_CONTRACT:
                from .segment_outputs import write_manifest

                segments = metadata.get("segments")
                if (
                    not isinstance(segments, list)
                    or [item.get("paragraph_id") for item in segments] != snapshot["paragraph_ids"]
                ):
                    raise TaskError(502, "adapter_invalid_output", "模型段落清单缺失或顺序不符")
                parts = destination.parent / "segments"
                parts.mkdir()
                with wave.open(str(native), "rb") as combined_native:
                    for item in segments:
                        identifier = item["paragraph_id"]
                        native_part = work / "native-segments" / f"{identifier}.wav"
                        if native_part.is_symlink() or digest_file(native_part) != item.get(
                            "sha256"
                        ):
                            raise TaskError(502, "adapter_invalid_output", "模型段落文件身份不符")
                        with wave.open(str(native_part), "rb") as source:
                            frames = source.getnframes()
                            pcm = source.readframes(frames)
                            if (
                                source.getparams()[:3] != combined_native.getparams()[:3]
                                or frames != item.get("frame_count")
                                or frames <= 0
                                or len(pcm)
                                != frames * source.getnchannels() * source.getsampwidth()
                                or pcm != combined_native.readframes(frames)
                            ):
                                raise TaskError(
                                    502, "adapter_invalid_output", "模型分段内容与整篇不符"
                                )
                        decode(native_part, parts / f"{identifier}.wav", ".wav", min_frames=1)
                    if combined_native.readframes(1):
                        raise TaskError(502, "adapter_invalid_output", "模型整篇存在未归属段落")
                with wave.open(str(destination), "wb") as combined:
                    combined.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    for identifier in snapshot["paragraph_ids"]:
                        with wave.open(str(parts / f"{identifier}.wav"), "rb") as part:
                            combined.writeframes(part.readframes(part.getnframes()))
                write_manifest(destination.parent, snapshot["paragraph_ids"])
            else:
                decode(native, destination, ".wav")
            self.last_result_metadata = {
                **expected,
                "model_id": config["model_id"],
                "model_revision": config["model_revision"],
                "native_sample_rate": metadata["native_sample_rate"],
                "model_loaded_at": metadata.get("model_loaded_at"),
                "effective_parameters": {},
            }
        finally:
            if process is not None:
                self.stop_process(process)
                try:
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                    if status.get("task_id") == self.task_id:
                        status.update(model_loaded=False, state="exited")
                        temporary = status_path.with_suffix(".tmp")
                        temporary.write_text(json.dumps(status), encoding="utf-8")
                        temporary.replace(status_path)
                except (OSError, ValueError):
                    logger.exception("模型子进程最终状态写入失败")
            shutil.rmtree(work, ignore_errors=True)
