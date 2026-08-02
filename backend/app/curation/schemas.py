from __future__ import annotations

import re
from typing import Any

import nh3
from pydantic import BaseModel, ConfigDict, Field, field_validator

UNSAFE_TEXT = re.compile(
    r"(?i)(?:https?://|ftp://|javascript:|data:|file:|www\.|\bignore\s+(?:all\s+)?(?:previous|prior)\s+instructions\b)"
)
TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,49}$")


class CurationRejected(ValueError):
    """Structured output failed validation and must not be persisted."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CurationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=2_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    reading_time_minutes: int = Field(ge=1, le=120)
    relevance_score: float = Field(ge=0.0, le=1.0)

    @field_validator("title", "summary")
    @classmethod
    def validate_plain_text(cls, value: str) -> str:
        if nh3.clean(value) != value or UNSAFE_TEXT.search(value):
            raise ValueError("curation text contains unsafe content")
        return value

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for tag in value:
            tag = tag.strip().lower()
            if not TAG_PATTERN.fullmatch(tag):
                raise ValueError("curation tag is invalid")
            if tag not in normalized:
                normalized.append(tag)
        return normalized


def validate_curation_output(payload: str | dict[str, Any]) -> CurationResult:
    try:
        if isinstance(payload, str):
            return CurationResult.model_validate_json(payload)
        return CurationResult.model_validate(payload)
    except ValueError as exc:
        raise CurationRejected("invalid_structured_output") from exc
