"""Browser acceptance uses disposable storage, never the user's data directory."""

import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from backend.app.main import create_app

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="voice-style-e2e-") as directory:
        os.environ["VOICE_PERSONA_CONFIG"] = str(Path(directory) / "no-persona.json")
        uvicorn.run(create_app(data_dir=directory), host="127.0.0.1",
                    port=int(os.environ.get("VOICE_E2E_PORT", "18765")), log_level="warning")
