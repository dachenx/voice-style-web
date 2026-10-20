"""Keep application tests independent from installed private persona assets."""

import pytest


@pytest.fixture(autouse=True)
def isolate_persona_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "VOICE_PERSONA_CONFIG", str(tmp_path / "unconfigured-persona" / "runtime.json")
    )
