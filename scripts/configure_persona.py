"""Register the explicitly selected, local frozen persona release."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.persona_config import build, config_path, install  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, required=True)
    args = parser.parse_args()
    value = build(args.release.resolve())
    install(value, config_path())
    print("Registered persona:", value["persona_id"], value["config_fingerprint"])
