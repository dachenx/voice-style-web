"""Replaceable text conversion registry; no production converter is installed."""

from collections.abc import Sequence
from typing import Protocol

from .text_models import ConversionRequest, ConversionResult, ConverterCapability


class TextConverter(Protocol):
    @property
    def capability(self) -> ConverterCapability: ...

    def convert(self, request: ConversionRequest) -> ConversionResult: ...


class ConverterRegistry:
    def __init__(self, converters: Sequence[TextConverter]) -> None:
        self.converters: dict[str, TextConverter] = {}
        for converter in converters:
            identifier = converter.capability.id
            if identifier in self.converters:
                raise ValueError("转换组件 ID 重复")
            self.converters[identifier] = converter

    def capabilities(self) -> list[dict[str, object]]:
        return [
            converter.capability.model_dump(mode="json") for converter in self.converters.values()
        ]
