"""Replaceable simulated and local speech generation contracts."""

import math
import struct
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .text_models import Revision, StrictModel

PARAGRAPH_OUTPUT_CONTRACT = "paragraphs-v1"


class TaskError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


class AdapterCapability(StrictModel):
    id: str
    version: str
    label: str
    is_simulated: bool
    output_kind: str
    modes: tuple[Literal["preview", "full"], ...]
    supported_parameters: tuple[str, ...] = ()


class RealAdapterCapability(AdapterCapability):
    requires_transcript: bool = True
    model_id: str
    model_revision: str
    model_fingerprint: str | None
    config_fingerprint: str | None
    environment_fingerprint: str | None = None
    reference_max_duration_ms: int = 30000
    reference_min_sample_rate: int = 16000
    max_text_tokens: int = 80
    max_reference_transcript_characters: int = 2000
    limit_note: str = "80 token 为本适配器首版操作限制，参考源码分段阈值；不是已测声音上限"


class TaskInput(StrictModel):
    request_id: UUID
    expected_revision: Revision
    expected_confirmation_id: UUID
    expected_clip_id: UUID | None = None
    adapter_id: str = Field(min_length=1, max_length=100)
    mode: Literal["preview", "full"]
    paragraph_id: UUID | None
    parameters: dict[str, Any]
    expected_transcript_revision: Revision | None = None
    expected_adapter_version: str | None = Field(default=None, min_length=1, max_length=100)
    expected_config_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("parameters")
    @classmethod
    def empty_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value:
            raise ValueError("当前适配器仅支持空参数，不支持语速或情绪设置")
        return value

    @model_validator(mode="after")
    def paragraph_mode(self) -> "TaskInput":
        if (self.mode == "preview") != (self.paragraph_id is not None):
            raise ValueError("短段模式须选择段落，全文模式不能指定单段")
        return self


class RetryInput(StrictModel):
    request_id: UUID


class GenerationAdapter(Protocol):
    @property
    def capability(self) -> AdapterCapability: ...

    def generate(
        self, snapshot: dict[str, Any], destination: Path, progress: Callable[[int], None]
    ) -> None: ...


class SimulatedToneAdapter:
    capability = AdapterCapability(
        id="simulated-tone",
        version="1",
        label="模拟测试音",
        is_simulated=True,
        output_kind="synthetic-tone",
        modes=("preview", "full"),
    )

    def generate(
        self, snapshot: dict[str, Any], destination: Path, progress: Callable[[int], None]
    ) -> None:
        # Each completed paragraph writes actual PCM samples; no timer-based fake percentage.
        sample_rate = 24000
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            for index, paragraph_id in enumerate(snapshot["paragraph_ids"], start=1):
                frequency = 330 + (index % 5) * 55
                samples = bytearray()
                for position in range(sample_rate // 4):
                    sample = int(4000 * math.sin(2 * math.pi * frequency * position / sample_rate))
                    samples.extend(struct.pack("<h", sample))
                output.writeframes(samples)
                if snapshot.get("output_contract") == PARAGRAPH_OUTPUT_CONTRACT:
                    parts = destination.parent / "segments"
                    parts.mkdir(exist_ok=True)
                    with wave.open(str(parts / f"{UUID(paragraph_id)}.wav"), "wb") as part:
                        part.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
                        part.writeframes(samples)
                progress(index)
        if snapshot.get("output_contract") == PARAGRAPH_OUTPUT_CONTRACT:
            from .segment_outputs import write_manifest

            write_manifest(destination.parent, snapshot["paragraph_ids"])


class GenerationRegistry:
    def __init__(
        self,
        adapters: Sequence[GenerationAdapter] | None = None,
        *,
        model_config_path: Path | None = None,
        runtime_directory: Path | None = None,
    ) -> None:
        from .local_model import LocalCosyVoiceAdapter
        from .local_qwen import LocalQwenAdapter

        self.adapters: dict[str, GenerationAdapter] = {}
        selected: Sequence[GenerationAdapter] = (
            adapters
            if adapters is not None
            else [
                SimulatedToneAdapter(),
                LocalCosyVoiceAdapter(model_config_path, runtime_directory=runtime_directory),
            ]
        )
        if adapters is None:
            from .persona_config import config_path

            if config_path().exists() or (config_path().parent / "persona-configs").is_dir():
                selected = [LocalQwenAdapter(runtime_directory=runtime_directory), *selected]
        for adapter in selected:
            if adapter.capability.id in self.adapters:
                raise ValueError("生成适配器 ID 重复")
            self.adapters[adapter.capability.id] = adapter

    def capabilities(self) -> list[dict[str, Any]]:
        return [adapter.capability.model_dump(mode="json") for adapter in self.adapters.values()]

    def require(self, adapter_id: str) -> GenerationAdapter:
        adapter = self.adapters.get(adapter_id)
        if adapter is None:
            raise TaskError(503, "adapter_unavailable", "生成适配器不可用")
        return adapter

    def for_snapshot(self, snapshot: dict[str, Any]) -> GenerationAdapter:
        from .local_model import LocalCosyVoiceAdapter
        from .local_qwen import LocalQwenAdapter

        adapter = self.require(snapshot["adapter"]["id"])
        if isinstance(adapter, (LocalCosyVoiceAdapter, LocalQwenAdapter)):
            return adapter.frozen(snapshot["adapter"]["config_fingerprint"])
        return adapter

    def health(self, adapter_id: str) -> dict[str, Any]:
        from .local_model import LocalCosyVoiceAdapter
        from .local_qwen import LocalQwenAdapter

        adapter = self.require(adapter_id)
        if isinstance(adapter, (LocalCosyVoiceAdapter, LocalQwenAdapter)):
            return adapter.health()
        return {
            "adapter_id": adapter_id,
            "configured": True,
            "configuration_valid": True,
            "execution_available": True,
            "model_loaded": False,
            "state": "ready",
            "error": None,
        }
