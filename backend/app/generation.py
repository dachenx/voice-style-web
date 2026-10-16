"""Explicitly simulated generation protocol; no speech model is installed."""

import math
import struct
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .text_models import Revision, StrictModel


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


class TaskInput(StrictModel):
    request_id: UUID
    expected_revision: Revision
    expected_confirmation_id: UUID
    expected_clip_id: UUID
    adapter_id: str = Field(min_length=1, max_length=100)
    mode: Literal["preview", "full"]
    paragraph_id: UUID | None
    parameters: dict[str, Any]

    @field_validator("parameters")
    @classmethod
    def empty_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value:
            raise ValueError("模拟适配器仅支持空参数，不支持语速或情绪设置")
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
            for index, _ in enumerate(snapshot["paragraph_ids"], start=1):
                frequency = 330 + (index % 5) * 55
                samples = bytearray()
                for position in range(sample_rate // 4):
                    sample = int(4000 * math.sin(2 * math.pi * frequency * position / sample_rate))
                    samples.extend(struct.pack("<h", sample))
                output.writeframes(samples)
                progress(index)


class GenerationRegistry:
    def __init__(self, adapters: Sequence[GenerationAdapter] | None = None) -> None:
        self.adapters: dict[str, GenerationAdapter] = {}
        for adapter in adapters if adapters is not None else [SimulatedToneAdapter()]:
            if adapter.capability.id in self.adapters:
                raise ValueError("生成适配器 ID 重复")
            if not adapter.capability.is_simulated:
                raise ValueError("M04 只允许模拟测试适配器")
            self.adapters[adapter.capability.id] = adapter

    def capabilities(self) -> list[dict[str, Any]]:
        return [adapter.capability.model_dump(mode="json") for adapter in self.adapters.values()]

    def require(self, adapter_id: str) -> GenerationAdapter:
        adapter = self.adapters.get(adapter_id)
        if adapter is None:
            raise TaskError(503, "adapter_unavailable", "模拟生成适配器不可用")
        return adapter
