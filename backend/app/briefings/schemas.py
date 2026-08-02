from __future__ import annotations

import re
from typing import Any

import nh3
from pydantic import BaseModel, ConfigDict, Field, field_validator

UNSAFE_TEXT = re.compile(r"(?i)(?:https?://|javascript:|data:|file:|\bignore\s+(?:all\s+)?(?:previous|prior)\s+instructions\b)")


class BriefingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=300)
    executive_summary: str = Field(min_length=1, max_length=2_000)
    key_points: list[str] = Field(min_length=1, max_length=8)
    caveat: str = Field(min_length=1, max_length=500)

    @field_validator("title", "executive_summary", "key_points", "caveat")
    @classmethod
    def validate_plain_text(cls, value: Any) -> Any:
        values = value if isinstance(value, list) else [value]
        for text in values:
            if not isinstance(text, str) or nh3.clean(text, tags=set()) != text or UNSAFE_TEXT.search(text):
                raise ValueError("briefing text contains unsafe content")
        return value


def validate_briefing_output(payload: str | dict[str, Any]) -> BriefingDraft:
    if isinstance(payload, str):
        return BriefingDraft.model_validate_json(payload)
    return BriefingDraft.model_validate(payload)
