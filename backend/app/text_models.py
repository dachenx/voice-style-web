"""Validated editable text, immutable confirmation and converter contracts."""

import unicodedata
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

Revision = Annotated[StrictInt, Field(ge=0)]


def safe_text(value: str) -> str:
    if any(unicodedata.category(char) == "Cc" and char not in "\n\t" for char in value):
        raise ValueError("文字不能包含换行和制表符以外的控制字符")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Paragraph(StrictModel):
    id: UUID
    original_text: str = Field(max_length=5000)
    draft_text: str = Field(max_length=5000)
    pronunciation_note: str = Field(max_length=500)

    _safe = field_validator("original_text", "draft_text", "pronunciation_note")(safe_text)


class TextContent(StrictModel):
    source_language: Literal["zh-CN"] = "zh-CN"
    target_language: Literal["wuhan"] = "wuhan"
    speech_register: str = Field(default="自然口语", max_length=80, alias="register")
    terms: str = Field(default="", max_length=2000)
    paragraphs: tuple[Paragraph, ...] = Field(default=(), max_length=200)

    _safe = field_validator("speech_register", "terms")(safe_text)

    @model_validator(mode="after")
    def validate_paragraphs(self) -> "TextContent":
        if len({paragraph.id for paragraph in self.paragraphs}) != len(self.paragraphs):
            raise ValueError("段落 ID 不能重复")
        for field in ("original_text", "draft_text"):
            if sum(len(getattr(paragraph, field)) for paragraph in self.paragraphs) > 20000:
                raise ValueError("原稿和草稿各不能超过 20000 字符")
        return self


class SaveText(TextContent):
    source_language: Literal["zh-CN"]
    target_language: Literal["wuhan"]
    speech_register: str = Field(max_length=80, alias="register")
    terms: str = Field(max_length=2000)
    paragraphs: tuple[Paragraph, ...] = Field(max_length=200)
    expected_revision: Revision


class ConfirmText(StrictModel):
    expected_revision: Revision
    acknowledged: StrictBool

    @field_validator("acknowledged")
    @classmethod
    def require_acknowledgement(cls, value: bool) -> bool:
        if not value:
            raise ValueError("须明确人工确认")
        return value


class ConvertText(StrictModel):
    expected_revision: Revision
    converter_id: str = Field(min_length=1, max_length=100)


class ConverterCapability(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    source_languages: tuple[str, ...]
    target_languages: tuple[str, ...]


class ConversionParagraph(StrictModel):
    id: UUID
    original_text: str


class ConversionRequest(StrictModel):
    source_language: str
    target_language: str
    speech_register: str = Field(alias="register")
    terms: str
    paragraphs: tuple[ConversionParagraph, ...]


class ConvertedParagraph(StrictModel):
    id: UUID
    draft_text: str = Field(max_length=5000)

    _safe = field_validator("draft_text")(safe_text)


class ConversionResult(StrictModel):
    paragraphs: tuple[ConvertedParagraph, ...] = Field(max_length=200)
    warnings: tuple[str, ...] = Field(default=(), max_length=200)

    @field_validator("warnings")
    @classmethod
    def safe_warnings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            safe_text(value)
            if len(value) > 2000:
                raise ValueError("转换提示过长")
        return values
