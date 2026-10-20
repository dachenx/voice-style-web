"""Controlled offline Qwen persona inference; model imports only in the child."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import traceback
import wave
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

try:
    from .cosyvoice_bridge import (
        BridgeFailure,
        install_parent_watchdog,
        sha256,
        write_json,
    )
except ImportError:
    from cosyvoice_bridge import (
        BridgeFailure,
        install_parent_watchdog,
        sha256,
        write_json,
    )


def require_icl(model):
    if getattr(model.config, "tts_model_type", None) != "base" or not getattr(
        model.speech_tokenizer, "has_encoder", False
    ):
        raise BridgeFailure("model_configuration_changed")


def mono_audio(audio):
    if audio.ndim == 1:
        return audio
    if audio.ndim == 2 and 1 in audio.shape:
        return audio.reshape(-1)
    raise BridgeFailure("invalid_model_output")


def run(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.app.model_config import canonical_config, digest_json
    from backend.app.persona_config import validate

    envelope = json.loads(args.input.read_text())
    config, snapshot = envelope["configuration"], envelope["snapshot"]
    validate(config, full_hash=True)
    actual = {
        name: importlib.metadata.version(name)
        for name in config["environment"]["packages"]
    }
    if (
        actual != config["environment"]["packages"]
        or sys.version.split()[0] != config["environment"]["python_version"]
    ):
        raise BridgeFailure("model_dependency_changed")
    reference = Path(envelope["reference_path"])
    if (
        sha256(reference) != config["reference_sha256"]
        or snapshot["reference"]["transcript"]["text"] != config["reference_text"]
    ):
        raise BridgeFailure("reference_changed")
    texts_by_id = {
        p["id"]: p["draft_text"] for p in snapshot["confirmation"]["paragraphs"]
    }
    texts = [texts_by_id[i] for i in snapshot["paragraph_ids"]]
    if any(
        not text.strip() or len(text) > config["limits"]["max_text_characters"]
        for text in texts
    ):
        raise BridgeFailure("text_limit_exceeded")
    os.environ["MLX_ENABLE_TF32"] = "0"
    status = {
        "pid": os.getpid(),
        "task_id": envelope["task_id"],
        "state": "loading",
        "model_loaded": False,
        "loaded_at": None,
        "config_fingerprint": config["config_fingerprint"],
    }
    write_json(args.status, status)
    import mlx.core as mx
    import mlx.nn as nn
    import numpy as np
    import soundfile as sf
    from mlx.utils import tree_flatten
    from mlx_audio.tts.utils import load_model

    mx.set_memory_limit(12 * 1024**3)
    mx.set_cache_limit(128 * 1024**2)

    class Adapter(nn.Module):
        def __init__(self, base, ind, outd):
            super().__init__()
            self.base = base
            self.a = mx.random.normal((ind, 8)) * 0.01
            self.b = mx.zeros((8, outd))

        def __call__(self, x):
            b = self.base(x)
            return b + ((x @ self.a @ self.b) * 2.0).astype(b.dtype)

    model = load_model(config["model_directory"])
    require_icl(model)
    model.talker.set_dtype(mx.float32)
    model.freeze()
    cls = type(model.talker)
    original = cls.__call__

    def forward(self, inputs_embeds, *a, **kw):
        return original(self, inputs_embeds.astype(mx.float32), *a, **kw)

    cls.__call__ = forward
    cfg = model.talker.config
    for layer in model.talker.model.layers:
        for name, dim in [
            ("q_proj", cfg.num_attention_heads * cfg.head_dim),
            ("v_proj", cfg.num_key_value_heads * cfg.head_dim),
        ]:
            setattr(
                layer.self_attn,
                name,
                Adapter(getattr(layer.self_attn, name), cfg.hidden_size, dim),
            )
    weights = mx.load(config["adapter_path"])
    if set(weights) != set(dict(tree_flatten(model.trainable_parameters()))):
        raise BridgeFailure("model_configuration_changed")
    model.load_weights(config["adapter_path"], strict=False)
    actual_weights = dict(tree_flatten(model.trainable_parameters()))
    if not all(
        bool(mx.array_equal(actual_weights[k], v).item()) for k, v in weights.items()
    ):
        raise BridgeFailure("model_configuration_changed")
    status.update(
        state="loaded",
        model_loaded=True,
        loaded_at=datetime.now(timezone.utc).isoformat(),
    )
    write_json(args.status, status)
    parts = args.output.parent / "native-segments"
    parts.mkdir()
    segments = []
    total = 0
    with wave.open(str(args.output), "wb") as combined:
        combined.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        for index, text in enumerate(texts, 1):
            status.update(state="generating", paragraph_index=index)
            write_json(args.status, status)
            mx.random.seed(config["seed"])
            results = list(
                model.generate(
                    text=text,
                    ref_audio=str(reference),
                    ref_text=config["reference_text"],
                    lang_code="Chinese",
                    stream=False,
                    **config["sampling"],
                )
            )
            if len(results) != 1 or results[0].sample_rate != 24000:
                raise BridgeFailure("invalid_model_output")
            if (
                getattr(results[0], "token_count", 512)
                >= config["sampling"]["max_tokens"]
            ):
                raise BridgeFailure("output_limit_exceeded")
            y = mono_audio(np.array(results[0].audio))
            if not y.size or not np.isfinite(y).all():
                raise BridgeFailure("invalid_model_output")
            if total + len(y) > 24000 * 600:
                raise BridgeFailure("output_limit_exceeded")
            identifier = str(UUID(snapshot["paragraph_ids"][index - 1]))
            part = parts / f"{identifier}.wav"
            sf.write(part, y, 24000, subtype="PCM_16")
            with wave.open(str(part)) as source:
                combined.writeframes(source.readframes(source.getnframes()))
            total += len(y)
            segments.append(
                {
                    "paragraph_id": identifier,
                    "sha256": sha256(part),
                    "frame_count": len(y),
                }
            )
            write_json(args.progress, {"completed_paragraphs": index})
    write_json(
        args.output.with_suffix(".meta.json"),
        {
            "input_sha256": hashlib.sha256(
                canonical_config(snapshot).encode()
            ).hexdigest(),
            "reference_sha256": sha256(reference),
            "config_fingerprint": config["config_fingerprint"],
            "model_fingerprint": config["model_fingerprint"],
            "environment_fingerprint": digest_json(config["environment"]),
            "native_sample_rate": 24000,
            "frame_count": total,
            "segments": segments,
            "model_loaded_at": status["loaded_at"],
        },
    )
    status.update(state="completed", model_loaded=False)
    write_json(args.status, status)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("input", "output", "progress", "status"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    install_parent_watchdog()
    try:
        run(args)
    except Exception as exc:
        traceback.print_exc()
        write_json(
            args.output.with_suffix(".error.json"),
            {
                "code": exc.code
                if isinstance(exc, BridgeFailure)
                else "model_inference_failed"
            },
        )
        sys.exit(1)
