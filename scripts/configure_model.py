"""Fingerprint an already-installed local model; never downloads or imports it."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from backend.app.model_config import build_config, save_config

    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()
    value = build_config(args.python, args.repository, args.model)
    save_config(value)
    print("model_fingerprint=" + value["model_fingerprint"])
    print("config_fingerprint=" + value["config_fingerprint"])
    print("model_files=" + str(len(value["files"])))
    print("source_files=" + str(len(value["source_files"])))
    print("Model/config files verified; model loading and speech quality are not verified.")
    print("Restart the local application to use this configuration.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        raise SystemExit(
            "Local model configuration failed; check the installed environment."
        ) from exc
