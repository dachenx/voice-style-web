"""Trusted subprocess fixture: generated silence is never model-quality evidence."""

import argparse
import json
import os
import subprocess
import sys
import time
import wave
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.model_config import canonical_config, digest_json  # noqa: E402
from backend.app.tasks import file_digest  # noqa: E402
from scripts.cosyvoice_bridge import install_parent_watchdog, write_json  # noqa: E402


def main() -> int:
    install_parent_watchdog()
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", type=Path, required=True)
    for name in ("input", "output", "progress", "status"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    value = json.loads(args.input.read_text())
    config, snapshot = value["configuration"], value["snapshot"]
    args.control.with_suffix(".pid").write_text(str(os.getpid()))
    args.control.with_suffix(".request.json").write_text(canonical_config(value))
    behavior = args.control.read_text() if args.control.exists() else "success"
    if behavior == "orphan":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        args.control.with_suffix(".child-pid").write_text(str(child.pid))
        return 1
    if behavior == "no-status":
        time.sleep(60)
        return 1
    status = dict(
        pid=os.getpid(),
        task_id=value["task_id"],
        config_fingerprint=config["config_fingerprint"],
        state="loaded",
        model_loaded=True,
        loaded_at=datetime.now(UTC).isoformat(),
    )
    write_json(args.status, status)
    if behavior == "block":
        time.sleep(60)
        return 1
    if behavior == "fail":
        write_json(args.output.with_suffix(".error.json"), {"code": "model_inference_failed"})
        return 1
    segments = []
    segmented = snapshot.get("output_contract") == "paragraphs-v1"
    if segmented:
        (args.output.parent / "native-segments").mkdir()
    with wave.open(str(args.output), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        for index, identifier in enumerate(snapshot["paragraph_ids"], start=1):
            frames = 24 if behavior == "short" and index == 1 else 6000
            output.writeframes(b"\0\0" * frames)
            if segmented:
                path = args.output.parent / "native-segments" / f"{identifier}.wav"
                with wave.open(str(path), "wb") as part:
                    part.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    part.writeframes(b"\0\0" * frames)
                segments.append(
                    {
                        "paragraph_id": identifier,
                        "sha256": file_digest(path),
                        "frame_count": frames,
                    }
                )
            write_json(args.progress, {"completed_paragraphs": index})
    write_json(
        args.output.with_suffix(".meta.json"),
        {
            "input_sha256": digest_json(snapshot),
            "reference_sha256": file_digest(Path(value["reference_path"])),
            "config_fingerprint": config["config_fingerprint"],
            "model_fingerprint": config["model_fingerprint"],
            "environment_fingerprint": digest_json(config["environment"]),
            "native_sample_rate": 24000,
            "model_loaded_at": status["loaded_at"],
            **({"segments": segments} if segmented else {}),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
