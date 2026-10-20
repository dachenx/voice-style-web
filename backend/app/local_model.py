"""Supervise an offline model subprocess without importing its dependencies."""

import json
import logging
import os
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .generation import RealAdapterCapability, TaskError
from .model_config import (
    MODEL_ID,
    MODEL_REVISION,
    ModelConfigError,
    digest_json,
    load_config,
    validate_config,
)
from .model_runner import ControlledModelRunner
from .storage import connect

logger = logging.getLogger(__name__)
BRIDGE = Path(__file__).resolve().parents[2] / "scripts/cosyvoice_bridge.py"
ERROR_MESSAGES = {
    "model_configuration_changed": "固定模型配置已改变，请核对本机环境",
    "model_dependency_changed": "固定模型的关键依赖版本已改变，未使用新环境替换原任务",
    "reference_changed": "固定参考音频内容已改变",
    "transcript_required": "固定参考片段缺少匹配的准确原话",
    "text_limit_exceeded": "当前段落超过本适配器80 token操作限额，请缩短段落并重新确认",
    "output_limit_exceeded": "模型输出超过本适配器资源限额",
    "invalid_model_output": "真实模型未返回完整有效音频",
    "model_inference_failed": "本机模型加载或推理失败，请查看本机诊断日志",
}


class LocalCosyVoiceAdapter(ControlledModelRunner):
    bridge_script = BRIDGE

    def __init__(
        self,
        path: Path | None = None,
        *,
        fingerprint: str | None = None,
        runtime_directory: Path | None = None,
        bridge_command: Sequence[str] | None = None,
        bridge_timeout: float | None = None,
    ) -> None:
        self.config_path = path
        self.runtime_directory = runtime_directory
        # Only trusted Python test code can override the runner; HTTP/config cannot set commands.
        self.bridge_command = tuple(bridge_command) if bridge_command is not None else None
        self.bridge_timeout = bridge_timeout
        self.configuration: dict[str, Any] | None = None
        self.invalid_configuration = False
        self.reference_path: Path | None = None
        self.lock_fd: int | None = None
        self.task_id: str | None = None
        self.should_stop: Callable[[], bool] = lambda: False
        self.last_result_metadata: dict[str, Any] = {}
        try:
            self.configuration = load_config(path, fingerprint)
        except ModelConfigError:
            self.invalid_configuration = True
        value = self.configuration
        self.capability = RealAdapterCapability(
            id="cosyvoice3-local",
            version="1",
            label="CosyVoice3 本机原速",
            is_simulated=False,
            output_kind="speech",
            modes=("preview", "full"),
            model_id=MODEL_ID,
            model_revision=MODEL_REVISION,
            model_fingerprint=value["model_fingerprint"] if value else None,
            config_fingerprint=value["config_fingerprint"] if value else None,
            environment_fingerprint=digest_json(value["environment"])
            if value and "environment" in value
            else None,
        )

    def frozen(self, fingerprint: str) -> "LocalCosyVoiceAdapter":
        adapter = LocalCosyVoiceAdapter(
            self.config_path,
            fingerprint=fingerprint,
            runtime_directory=self.runtime_directory,
            bridge_command=self.bridge_command,
            bridge_timeout=self.bridge_timeout,
        )
        adapter.require_configuration()
        return adapter

    def require_configuration(self) -> dict[str, Any]:
        if self.configuration is None:
            raise TaskError(503, "model_configuration_unavailable", "固定本机模型配置缺失或无效")
        try:
            validate_config(self.configuration)
        except ModelConfigError as exc:
            raise TaskError(
                503, "model_configuration_changed", "本机模型或关键环境记录已改变，请核对配置"
            ) from exc
        return self.configuration

    def health(self) -> dict[str, Any]:
        valid = False
        if self.configuration is not None:
            try:
                self.require_configuration()
                valid = True
            except TaskError:
                pass
        available = valid and os.name == "posix" and BRIDGE.is_file()
        loaded = False
        loaded_at = None
        if self.runtime_directory is not None and self.capability.config_fingerprint:
            try:
                status = json.loads(
                    (
                        self.runtime_directory / f"{self.capability.config_fingerprint}.json"
                    ).read_text(encoding="utf-8")
                )
                loaded_at = status.get("loaded_at")
                if status.get("model_loaded") is True and status.get("state") in (
                    "loaded",
                    "generating",
                ):
                    with connect(self.runtime_directory.parent / "projects.sqlite3") as db:
                        task = db.execute(
                            "SELECT snapshot_json FROM generation_tasks "
                            "WHERE id=? AND state='running'",
                            (status.get("task_id"),),
                        ).fetchone()
                    if task and json.loads(task[0])["adapter"]["config_fingerprint"] == (
                        status.get("config_fingerprint")
                    ):
                        os.kill(status["pid"], 0)
                        loaded = True
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                pass
        state = (
            "loaded"
            if loaded
            else "ready"
            if available
            else "configuration_invalid"
            if self.invalid_configuration or self.configuration
            else "not_configured"
        )
        return {
            "adapter_id": self.capability.id,
            "configured": self.configuration is not None,
            "configuration_valid": valid,
            "execution_available": available,
            "model_loaded": loaded,
            "last_loaded_at": loaded_at,
            "state": state,
            "verification_scope": (
                "配置检查仅核对文件元数据；执行时校验内容与关键依赖版本，"
                "加载状态为当前运行任务的最近进程报告，不代表整个环境不可变或声音达标"
            ),
            "error": None
            if available
            else {"code": state, "message": "本机模型配置或受控执行环境不可用"},
        }
