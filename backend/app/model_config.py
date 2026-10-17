"""Local, ignored runtime manifests. Lightweight health never claims model loading."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

MODEL_ID = "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
MODEL_REVISION = "29e01c4e8d000f4bcd70751be16fa94bf3d85a18"
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / ".quality-local" / "model-runtime.json"
LIMITS = {
    "reference_max_duration_ms": 30000,
    "reference_min_sample_rate": 16000,
    "max_text_tokens": 80,
    "max_reference_transcript_characters": 2000,
    "paragraph_timeout_seconds": 600,
    "task_timeout_seconds": 3600,
    "model_load_timeout_seconds": 180,
}
REQUIRED_MODEL_FILES = (
    "llm.pt",
    "flow.pt",
    "hift.pt",
    "campplus.onnx",
    "speech_tokenizer_v3.onnx",
    "cosyvoice3.yaml",
    "config.json",
    "configuration.json",
    "CosyVoice-BlankEN/config.json",
    "CosyVoice-BlankEN/generation_config.json",
    "CosyVoice-BlankEN/merges.txt",
    "CosyVoice-BlankEN/model.safetensors",
    "CosyVoice-BlankEN/tokenizer_config.json",
    "CosyVoice-BlankEN/vocab.json",
)
CRITICAL_PACKAGES = (
    "torch",
    "torchaudio",
    "transformers",
    "tokenizers",
    "onnxruntime",
    "numpy",
    "HyperPyYAML",
    "modelscope",
    "librosa",
    "scipy",
    "soundfile",
    "omegaconf",
)


def package_versions(python: Path) -> dict[str, Any]:
    code = (
        "import importlib.metadata as m,json,sys\nversions={}\n"
        "for name in sys.argv[1:]:\n try: versions[name]=m.version(name)\n"
        " except m.PackageNotFoundError: versions[name]=None\n"
        "print(json.dumps({'python_version':sys.version.split()[0],'packages':versions}))"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", code, *CRITICAL_PACKAGES],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        value: dict[str, Any] = json.loads(result.stdout)
        return value
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ModelConfigError("无法读取独立模型环境的关键依赖版本") from exc


class ModelConfigError(ValueError):
    pass


def canonical_config(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_config(value).encode("utf-8")).hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def config_path(path: Path | None = None) -> Path:
    return (
        path
        if path is not None
        else Path(os.environ.get("VOICE_MODEL_CONFIG", str(DEFAULT_CONFIG)))
    )


def inventory(path: Path, base: Path) -> dict[str, Any]:
    actual = path.resolve()
    if not actual.is_relative_to(base.resolve()):
        raise ModelConfigError("模型或源码文件不能链接到配置目录之外")
    stat = path.stat()
    return {
        "path": str(path.relative_to(base)),
        "byte_size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest_file(path),
    }


def build_config(python: Path, repository: Path, model: Path) -> dict[str, Any]:
    if not all(path.is_absolute() for path in (python, repository, model)):
        raise ModelConfigError("模型环境必须使用绝对路径")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ModelConfigError("独立 Python 环境不可执行")
    if (
        not (repository / "cosyvoice/cli/cosyvoice.py").is_file()
        or not (repository / "third_party/Matcha-TTS").is_dir()
    ):
        raise ModelConfigError("本机 CosyVoice 源码目录不完整")
    if not all((model / name).is_file() for name in REQUIRED_MODEL_FILES):
        raise ModelConfigError("模型目录缺少必需权重或配置")
    files = [
        inventory(path, model)
        for path in sorted(model.rglob("*"))
        if path.is_file()
        and not {".cache", ".git", "__pycache__"}.intersection(path.relative_to(model).parts)
    ]
    sources = [
        inventory(path, repository)
        for directory in (repository / "cosyvoice", repository / "third_party/Matcha-TTS/matcha")
        for path in sorted(directory.rglob("*.py"))
    ]
    python_stat = python.stat()
    value = {
        "schema_version": 1,
        "python_executable": str(python),
        "repository": str(repository),
        "model_directory": str(model),
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "files": files,
        "source_files": sources,
        "python_identity": {
            "byte_size": python_stat.st_size,
            "mtime_ns": python_stat.st_mtime_ns,
            "sha256": digest_file(python),
        },
        "runtime": {
            "device": "cpu",
            "threads": 6,
            "fp16": False,
            "load_trt": False,
            "load_vllm": False,
            "stream": False,
            "text_frontend": False,
            "speed": 1.0,
        },
        "limits": LIMITS,
        "environment": package_versions(python),
        "model_fingerprint": digest_json(
            [{key: item[key] for key in ("path", "byte_size", "sha256")} for item in files]
        ),
    }
    value["config_fingerprint"] = digest_json(value)
    return value


def save_config(value: dict[str, Any], path: Path | None = None) -> Path:
    current = config_path(path)
    archive = current.parent / "model-configs" / f"{value['config_fingerprint']}.json"
    archive.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_config(value) + "\n"
    if archive.exists() and archive.read_text(encoding="utf-8") != encoded:
        raise ModelConfigError("同一配置指纹对应的私有内容不同")
    if not archive.exists():
        temporary = archive.with_suffix(".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(archive)
    temporary = current.with_suffix(".tmp")
    temporary.write_text(
        canonical_config({"config_fingerprint": value["config_fingerprint"]}) + "\n",
        encoding="utf-8",
    )
    temporary.replace(current)
    return current


def load_config(path: Path | None = None, fingerprint: str | None = None) -> dict[str, Any] | None:
    current = config_path(path)
    if not current.exists() and fingerprint is None:
        return None
    try:
        if fingerprint is None:
            pointer = json.loads(current.read_text(encoding="utf-8"))
            if not isinstance(pointer, dict) or set(pointer) != {"config_fingerprint"}:
                raise ModelConfigError("模型配置指针格式无效")
            fingerprint = pointer["config_fingerprint"]
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
        ):
            raise ModelConfigError("模型配置指纹无效")
        value = json.loads(
            (current.parent / "model-configs" / f"{fingerprint}.json").read_text(encoding="utf-8")
        )
        if not isinstance(value, dict) or value.get("config_fingerprint") != fingerprint:
            raise ModelConfigError("模型配置内容与指纹不匹配")
        body = {key: item for key, item in value.items() if key != "config_fingerprint"}
        if digest_json(body) != fingerprint:
            raise ModelConfigError("模型私有配置已改变，请重新配置")
        if (
            value.get("schema_version") != 1
            or value.get("model_id") != MODEL_ID
            or value.get("model_revision") != MODEL_REVISION
        ):
            raise ModelConfigError("模型配置身份不受支持")
        return value
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise ModelConfigError("本机模型配置不可用，请重新运行配置工具") from exc


def validate_config(value: dict[str, Any], *, full_hash: bool = False) -> None:
    try:
        python = Path(value["python_executable"])
        repository, model = Path(value["repository"]), Path(value["model_directory"])
        if not all(path.is_absolute() for path in (python, repository, model)) or not os.access(
            python, os.X_OK
        ):
            raise ModelConfigError("独立模型环境不可用")
        items = [(python, value["python_identity"])]
        if "environment" not in value:
            raise ModelConfigError("模型配置缺少关键依赖版本，请重新配置")
        if not set(REQUIRED_MODEL_FILES).issubset({item["path"] for item in value["files"]}):
            raise ModelConfigError("模型配置清单缺少必需文件")
        if value["model_fingerprint"] != digest_json(
            [{key: item[key] for key in ("path", "byte_size", "sha256")} for item in value["files"]]
        ):
            raise ModelConfigError("模型内容摘要与清单不匹配")
        for base, key in ((model, "files"), (repository, "source_files")):
            for item in value[key]:
                relative = Path(item["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ModelConfigError("模型清单路径无效")
                path = base / relative
                if not path.resolve().is_relative_to(base.resolve()):
                    raise ModelConfigError("模型清单路径越界")
                items.append((path, item))
        for path, item in items:
            stat = path.stat()
            if stat.st_size != item["byte_size"] or stat.st_mtime_ns != item["mtime_ns"]:
                raise ModelConfigError("模型环境文件已改变，请重新配置")
            if full_hash and digest_file(path) != item["sha256"]:
                raise ModelConfigError("模型环境内容已改变，请重新配置")
    except (OSError, KeyError, TypeError) as exc:
        raise ModelConfigError("本机模型环境不完整或不可读") from exc
