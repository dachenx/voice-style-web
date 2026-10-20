"""Fixed-persona Qwen adapter; shares process supervision, not CosyVoice semantics."""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from . import persona_config
from .generation import AdapterCapability, TaskError
from .model_config import ModelConfigError, digest_json
from .model_runner import ControlledModelRunner
from .storage import connect


class PersonaCapability(AdapterCapability):
    reference_source: str = "persona"
    requires_transcript: bool = False
    model_id: str
    model_revision: str
    model_fingerprint: str | None
    config_fingerprint: str | None
    environment_fingerprint: str | None
    max_text_characters: int = 80
    limit_note: str = "每段最多80个文字字符，为初版操作限额；不自动拆分，效果仍需试听"


class LocalQwenAdapter(ControlledModelRunner):
    bridge_script = Path(__file__).resolve().parents[2] / "scripts/qwen_bridge.py"

    def __init__(
        self,
        path: Path | None = None,
        *,
        fingerprint: str | None = None,
        runtime_directory: Path | None = None,
    ) -> None:
        self.config_path = path or persona_config.config_path()
        self.runtime_directory = runtime_directory
        self.configuration = None
        self.invalid_configuration = False
        self.reference_path = None
        self.lock_fd = None
        self.task_id = None
        self.should_stop = lambda: False
        self.bridge_command = None
        self.bridge_timeout = None
        self.last_result_metadata = {}
        try:
            self.configuration = persona_config.load(self.config_path, fingerprint)
        except ModelConfigError:
            self.invalid_configuration = True
        config = self.configuration
        self.capability = PersonaCapability(
            id=persona_config.PERSONA_ID,
            version="0.11",
            label=persona_config.LABEL,
            is_simulated=False,
            output_kind="speech",
            modes=("preview", "full"),
            model_id=config["model_id"]
            if config
            else "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
            model_revision=config["model_revision"] if config else "unconfigured",
            model_fingerprint=config["model_fingerprint"] if config else None,
            config_fingerprint=config["config_fingerprint"] if config else None,
            environment_fingerprint=digest_json(config["environment"]) if config else None,
        )

    def require_configuration(self) -> dict[str, Any]:
        if self.configuration is None:
            raise TaskError(503, "persona_unavailable", "人物版本未配置或资源不完整")
        try:
            persona_config.validate(self.configuration)
        except ModelConfigError as exc:
            raise TaskError(
                409, "persona_changed", "人物版本资源已改变，未使用其他声音替代"
            ) from exc
        return self.configuration

    def frozen(self, fingerprint: str) -> "LocalQwenAdapter":
        adapter = LocalQwenAdapter(
            self.config_path, fingerprint=fingerprint, runtime_directory=self.runtime_directory
        )
        adapter.require_configuration()
        return adapter

    def reference(self) -> dict[str, Any]:
        return persona_config.reference_snapshot(self.require_configuration())

    def health(self) -> dict[str, Any]:
        valid = False
        try:
            self.require_configuration()
            valid = True
        except TaskError:
            pass
        available = valid and os.name == "posix" and self.bridge_script.is_file()
        loaded = False
        loaded_at = None
        if self.runtime_directory and self.configuration:
            try:
                status = json.loads(
                    (
                        self.runtime_directory
                        / (self.configuration["config_fingerprint"] + ".json")
                    ).read_text()
                )
                loaded_at = status.get("loaded_at")
                if status.get("model_loaded") and status.get("state") in ("loaded", "generating"):
                    with connect(self.runtime_directory.parent / "projects.sqlite3") as db:
                        row = db.execute(
                            "SELECT snapshot_json FROM generation_tasks "
                            "WHERE id=? AND state='running'",
                            (status.get("task_id"),),
                        ).fetchone()
                    if (
                        row
                        and json.loads(row[0])["adapter"]["config_fingerprint"]
                        == self.capability.config_fingerprint
                    ):
                        os.kill(status["pid"], 0)
                        loaded = True
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                pass
        return {
            "adapter_id": self.capability.id,
            "configured": self.configuration is not None,
            "configuration_valid": valid,
            "execution_available": available,
            "model_loaded": loaded,
            "last_loaded_at": loaded_at,
            "state": "loaded" if loaded else "ready" if available else "unavailable",
            "error": None
            if available
            else {
                "code": "persona_unavailable",
                "message": "人物模型未配置或固定资源已改变，请检查本机人物版本；不会自动换声音",
            },
        }
