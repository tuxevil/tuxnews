from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import Settings

USER_SETTINGS_VERSION = 1
LLMProfileName = Literal["eco", "cloud", "hybrid"]


class UserScoreWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantic: float = Field(ge=0.0, le=1.0)
    reputation: float = Field(ge=0.0, le=1.0)
    feedback: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def require_positive_total(self) -> UserScoreWeights:
        if self.semantic + self.reputation + self.feedback <= 0:
            raise ValueError("at least one score weight must be positive")
        return self


class UserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=USER_SETTINGS_VERSION)
    llm_profile: LLMProfileName
    score_weights: UserScoreWeights
    score_words_per_minute: int = Field(ge=50, le=1_000)
    discovery_max_queries: int = Field(ge=1, le=32)
    discovery_max_candidates: int = Field(ge=1, le=500)
    briefing_max_items: int = Field(ge=1, le=50)


class UserSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = Field(default=None, ge=USER_SETTINGS_VERSION)
    llm_profile: LLMProfileName | None = None
    score_weights: UserScoreWeights | None = None
    score_words_per_minute: int | None = Field(default=None, ge=50, le=1_000)
    discovery_max_queries: int | None = Field(default=None, ge=1, le=32)
    discovery_max_candidates: int | None = Field(default=None, ge=1, le=500)
    briefing_max_items: int | None = Field(default=None, ge=1, le=50)


def _defaults(settings: Settings) -> dict[str, Any]:
    return {
        "version": USER_SETTINGS_VERSION,
        "llm_profile": settings.llm_default_profile,
        "score_weights": {
            "semantic": settings.score_semantic_weight,
            "reputation": settings.score_reputation_weight,
            "feedback": settings.score_feedback_weight,
        },
        "score_words_per_minute": settings.score_words_per_minute,
        "discovery_max_queries": settings.discovery_max_queries,
        "discovery_max_candidates": settings.discovery_max_candidates,
        "briefing_max_items": settings.briefing_max_items,
    }


def _overrides(document: dict[str, Any] | None) -> dict[str, Any]:
    if not document:
        return {}
    raw_overrides = document.get("overrides", {})
    if not isinstance(raw_overrides, dict):
        raise ValueError("user settings overrides must be an object")
    return dict(raw_overrides)


def resolve_user_settings(document: dict[str, Any] | None, settings: Settings | None = None) -> UserSettings:
    runtime = settings or Settings()
    raw = _defaults(runtime)
    raw.update(_overrides(document))
    if document and isinstance(document.get("version"), int):
        raw["version"] = document["version"]
    resolved = UserSettings.model_validate(raw)
    if resolved.discovery_max_queries > runtime.discovery_max_queries:
        raise ValueError("discovery query limit exceeds the runtime safety cap")
    if resolved.discovery_max_candidates > runtime.discovery_max_candidates:
        raise ValueError("discovery candidate limit exceeds the runtime safety cap")
    if resolved.briefing_max_items > runtime.briefing_max_items:
        raise ValueError("briefing item limit exceeds the runtime safety cap")
    return resolved


def update_user_settings_document(
    document: dict[str, Any] | None,
    update: UserSettingsUpdate,
    settings: Settings | None = None,
) -> tuple[dict[str, Any], UserSettings, list[str]]:
    runtime = settings or Settings()
    current = resolve_user_settings(document, runtime)
    if update.version is not None and update.version != current.version:
        raise ValueError("user settings version conflict")
    changed = update.model_dump(exclude_unset=True, exclude_none=True, exclude={"version"})
    if not changed:
        raise ValueError("at least one user setting must be provided")
    overrides = _overrides(document)
    overrides.update(changed)
    next_document = {"version": current.version + 1, "overrides": overrides}
    resolved = resolve_user_settings(next_document, runtime)
    return next_document, resolved, sorted(changed)
