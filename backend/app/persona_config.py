"""Immutable local persona configuration. Never loads a speech model in the API."""

import json
import os
import subprocess
import wave
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .model_config import ModelConfigError, digest_file, digest_json

PERSONA_ID = "wan-qwen3-v0.11"
LABEL = "万先生-Qwen3v0.11-武汉话"
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / ".quality-local/persona-runtime.json"
PACKAGES = ("mlx", "mlx-metal", "mlx-audio", "numpy", "soundfile", "transformers")


def config_path() -> Path:
    return Path(os.environ.get("VOICE_PERSONA_CONFIG", str(DEFAULT_CONFIG)))


def environment(python: Path) -> dict[str, Any]:
    code = (
        "import sys,json,importlib.metadata as m;"
        "print(json.dumps({'python_version':sys.version.split()[0],"
        "'packages':{n:m.version(n) for n in sys.argv[1:]}}))"
    )
    result = subprocess.run(
        [str(python), "-c", code, *PACKAGES], check=True, capture_output=True, text=True, timeout=15
    )
    return dict(json.loads(result.stdout))


def identity(path: Path) -> dict[str, Any]:
    path = path.resolve(strict=True)
    stat = path.stat()
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest_file(path),
    }


def build(release: Path) -> dict[str, Any]:
    manifest = json.loads((release / "manifest.json").read_text())
    files = []
    for item in json.loads(Path(manifest["base_inventory"]).read_text()):
        value = identity(Path(manifest["model_path"]) / item["path"])
        if value["sha256"] != item["sha256"]:
            raise ModelConfigError("底模摘要与交接不一致")
        files.append(value)
    base_files = list(files)
    for key in ("adapter", "reference"):
        value = identity(Path(manifest[key + "_path"]))
        if value["sha256"] != manifest[key + "_sha256"]:
            raise ModelConfigError("人物权重或参考摘要不一致")
        files.append(value)
    for item in json.loads((release / "mlx-audio源码摘要.json").read_text()):
        value = identity(Path(item["path"]))
        if value["sha256"] != item["sha256"]:
            raise ModelConfigError("模型调用源码与交接不一致")
        files.append(value)
    files.append(identity(Path(manifest["python"])))
    files.append(identity(Path(__file__).resolve().parents[2] / "scripts/qwen_bridge.py"))
    with wave.open(manifest["reference_path"]) as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (
            24000,
            1,
            2,
        ) or audio.getnframes() % 24:
            raise ModelConfigError(
                "Fixed reference must be mono PCM16 24kHz with whole milliseconds"
            )
        duration_ms = audio.getnframes() // 24
    value = {
        "schema_version": 1,
        "persona_id": PERSONA_ID,
        "label": LABEL,
        "model_id": manifest["model_id"],
        "model_revision": manifest["revision"],
        "model_directory": manifest["model_path"],
        "adapter_path": manifest["adapter_path"],
        "reference_path": manifest["reference_path"],
        "reference_text": manifest["reference_text"],
        "reference_sha256": manifest["reference_sha256"],
        "reference_duration_ms": duration_ms,
        "python_executable": manifest["python"],
        "files": files,
        "model_fingerprint": digest_json(
            {"base": base_files, "adapter": manifest["adapter_sha256"]}
        ),
        "environment": environment(Path(manifest["python"])),
        "sampling": manifest["sampling"],
        "seed": manifest["seed"],
        "actual_icl_repetition_penalty": manifest["actual_icl_repetition_penalty"],
        "limits": {
            "max_text_characters": 80,
            "task_timeout_seconds": 3600,
            "paragraph_timeout_seconds": 300,
            "model_load_timeout_seconds": 180,
        },
    }
    value["config_fingerprint"] = digest_json(value)
    validate(value, full_hash=True)
    return value


def validate(value: dict[str, Any], *, full_hash: bool = False) -> None:
    try:
        for key in (
            "model_directory",
            "adapter_path",
            "reference_path",
            "python_executable",
            "model_id",
            "model_revision",
            "model_fingerprint",
            "reference_sha256",
        ):
            if not isinstance(value[key], str) or not value[key]:
                raise ValueError("missing configuration field")
        if (
            not value["files"]
            or not value["environment"]["packages"]
            or value["limits"]["max_text_characters"] != 80
            or value["seed"] != 42
            or value["actual_icl_repetition_penalty"] != 1.5
            or value["sampling"]
            != {
                "temperature": 0.9,
                "top_p": 1.0,
                "top_k": 50,
                "repetition_penalty": 1.05,
                "max_tokens": 512,
            }
        ):
            raise ValueError("unsupported persona settings")
        if (
            value["persona_id"] != PERSONA_ID
            or value["schema_version"] != 1
            or digest_json({k: v for k, v in value.items() if k != "config_fingerprint"})
            != value["config_fingerprint"]
        ):
            raise ValueError("identity")
        for item in value["files"]:
            path = Path(item["path"])
            stat = path.stat()
            if (
                stat.st_size != item["size"]
                or stat.st_mtime_ns != item["mtime_ns"]
                or (full_hash and digest_file(path) != item["sha256"])
            ):
                raise ValueError("asset changed")
        if (
            not isinstance(value["reference_duration_ms"], int)
            or value["reference_duration_ms"] <= 0
        ):
            raise ValueError("invalid reference duration")
        for key in (
            "task_timeout_seconds",
            "paragraph_timeout_seconds",
            "model_load_timeout_seconds",
        ):
            if not isinstance(value["limits"][key], int) or value["limits"][key] <= 0:
                raise ValueError("invalid timeout")
        if not isinstance(value["reference_text"], str) or not value["reference_text"].strip():
            raise ValueError("missing reference transcript")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ModelConfigError("人物模型资产或固定配置缺失/改变") from exc


def install(value: dict[str, Any], path: Path) -> None:
    validate(value, full_hash=True)
    archive = path.parent / "persona-configs"
    archive.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    target = archive / (value["config_fingerprint"] + ".json")
    if target.exists() and target.read_text() != serialized:
        raise ModelConfigError("不能覆盖已有固定版本")
    # An existing persona ID may not silently change its dependencies.
    if (
        path.exists()
        and json.loads(path.read_text())["config_fingerprint"] != value["config_fingerprint"]
    ):
        raise ModelConfigError("人物版本已登记，请为不同依赖建立新版本")
    if not target.exists():
        temporary = target.with_suffix(".tmp")
        temporary.write_text(serialized)
        temporary.replace(target)
    temp = path.with_suffix(".tmp")
    temp.write_text(serialized)
    temp.replace(path)


def load(path: Path, fingerprint: str | None = None) -> dict[str, Any] | None:
    source = (
        path if fingerprint is None else path.parent / "persona-configs" / f"{fingerprint}.json"
    )
    if not source.exists():
        return None
    try:
        value: dict[str, Any] = json.loads(source.read_text())
        validate(value)
        if fingerprint and value["config_fingerprint"] != fingerprint:
            raise ModelConfigError("固定版本不匹配")
        return value
    except (OSError, ValueError) as exc:
        raise ModelConfigError("人物配置无法读取") from exc


def reference_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    identifier = str(uuid5(NAMESPACE_URL, config["config_fingerprint"]))
    digest = config["reference_sha256"]
    return {
        "id": identifier,
        "filename": "人物版本固定参考",
        "clip_sha256": digest,
        "clip": {
            "id": identifier,
            "reference_id": identifier,
            "start_ms": 0,
            "end_ms": config["reference_duration_ms"],
        },
        "transcript": {
            "text": config["reference_text"],
            "revision": 1,
            "clip_id": identifier,
            "clip_sha256": digest,
        },
    }
