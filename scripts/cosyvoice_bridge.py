"""Controlled, offline model subprocess. Importing this module never loads Torch."""

import argparse
import hashlib
import json
import os
import signal
import sys
import threading
import traceback
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BridgeFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
    temporary.replace(path)


def install_parent_watchdog() -> None:
    def parent_closed() -> None:
        try:
            os.read(0, 1)
        finally:
            if os.name == "posix" and os.getpgrp() == os.getpid():
                os.killpg(os.getpgrp(), signal.SIGKILL)
            os._exit(143)

    threading.Thread(target=parent_closed, daemon=True).start()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_text_tokens(model: Any, texts: list[str], limit: int) -> None:
    for text in texts:
        tokens = model.frontend.tokenizer.encode(
            text, allowed_special=model.frontend.allowed_special
        )
        if not text.strip() or len(tokens) > limit:
            raise BridgeFailure("text_limit_exceeded")


def run(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from backend.app.model_config import canonical_config, digest_json, validate_config

    envelope = json.loads(args.input.read_text(encoding="utf-8"))
    config, snapshot = envelope["configuration"], envelope["snapshot"]
    if (
        digest_json({key: value for key, value in config.items() if key != "config_fingerprint"})
        != config["config_fingerprint"]
    ):
        raise BridgeFailure("model_configuration_changed")
    validate_config(config, full_hash=True)
    import importlib.metadata

    actual_versions: dict[str, Any] = {}
    for name in config["environment"]["packages"]:
        try:
            actual_versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual_versions[name] = None
    if (
        actual_versions != config["environment"]["packages"]
        or sys.version.split()[0] != config["environment"]["python_version"]
    ):
        raise BridgeFailure("model_dependency_changed")
    reference = Path(envelope["reference_path"])
    if sha256(reference) != snapshot["reference"]["clip_sha256"]:
        raise BridgeFailure("reference_changed")
    transcript = snapshot["reference"]["transcript"]
    if (
        transcript["clip_sha256"] != snapshot["reference"]["clip_sha256"]
        or not transcript["text"].strip()
    ):
        raise BridgeFailure("transcript_required")
    os.chdir(config["repository"])
    sys.path.insert(0, config["repository"])
    sys.path.insert(0, str(Path(config["repository"]) / "third_party/Matcha-TTS"))
    status = {
        "pid": os.getpid(),
        "task_id": envelope["task_id"],
        "state": "loading",
        "model_loaded": False,
        "loaded_at": None,
        "config_fingerprint": config["config_fingerprint"],
    }
    write_json(args.status, status)
    import torch
    from cosyvoice.cli.cosyvoice import AutoModel

    torch.set_num_threads(config["runtime"]["threads"])
    model = AutoModel(
        model_dir=config["model_directory"], fp16=False, load_trt=False, load_vllm=False
    )
    status.update(
        state="loaded",
        model_loaded=True,
        # The independently installed model runtime is Python 3.10.
        loaded_at=datetime.now(timezone.utc).isoformat(),  # noqa: UP017
    )
    write_json(args.status, status)
    paragraphs = {item["id"]: item for item in snapshot["confirmation"]["paragraphs"]}
    texts = [paragraphs[identifier]["draft_text"] for identifier in snapshot["paragraph_ids"]]
    validate_text_tokens(model, texts, config["limits"]["max_text_tokens"])
    sample_rate = int(model.sample_rate)
    total_frames = 0
    with wave.open(str(args.output), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        for index, text in enumerate(texts, start=1):
            status.update(state="generating", paragraph_index=index)
            write_json(args.status, status)
            produced = 0
            for chunk in model.inference_zero_shot(
                text,
                "You are a helpful assistant.<|endofprompt|>" + transcript["text"],
                str(reference),
                stream=False,
                text_frontend=False,
                speed=1.0,
            ):
                samples = chunk["tts_speech"].detach().cpu().reshape(-1)
                if not bool(torch.isfinite(samples).all()):
                    raise BridgeFailure("invalid_model_output")
                frames = int(samples.numel())
                if total_frames + frames > sample_rate * 600:
                    raise BridgeFailure("output_limit_exceeded")
                pcm = (samples.clamp(-1, 1) * 32767).to(torch.int16).numpy().astype("<i2").tobytes()
                output.writeframes(pcm)
                total_frames += frames
                produced += frames
            if produced == 0:
                raise BridgeFailure("invalid_model_output")
            write_json(args.progress, {"completed_paragraphs": index})
    write_json(
        args.output.with_suffix(".meta.json"),
        {
            "input_sha256": hashlib.sha256(canonical_config(snapshot).encode()).hexdigest(),
            "reference_sha256": sha256(reference),
            "config_fingerprint": config["config_fingerprint"],
            "model_fingerprint": config["model_fingerprint"],
            "environment_fingerprint": digest_json(config["environment"]),
            "native_sample_rate": sample_rate,
            "frame_count": total_frames,
            "model_loaded_at": status["loaded_at"],
        },
    )
    status.update(state="completed", model_loaded=False)
    write_json(args.status, status)


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("input", "output", "progress", "status"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    install_parent_watchdog()
    try:
        run(args)
    except Exception as exc:
        traceback.print_exc()
        code = exc.code if isinstance(exc, BridgeFailure) else "model_inference_failed"
        write_json(args.output.with_suffix(".error.json"), {"code": code})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
