import re
from datetime import datetime
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime configuration shared by the API and workers."""

    model_config = SettingsConfigDict(
        env_prefix="TUXNEWS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Tuxnews"
    environment: str = "local"
    debug: bool = False
    observability_hash_salt: str = "local-only-change-me-observability-salt"
    health_check_timeout_seconds: float = 2.0
    api_prefix: str = "/api/v1"
    api_version: str = "1.1.0"
    database_url: str = "postgresql+asyncpg://tuxnews:tuxnews@postgres:5432/tuxnews"
    redis_url: str = "redis://redis:6379/0"
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection_prefix: str = "tuxnews"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_version: str = "v1"
    embedding_dimension: int = 384
    archive_root: str = "/news-archive"
    sources_path: str = "sources.yaml"
    allowed_cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1", "testserver"])
    hsts_enabled: bool = False
    access_token_secret: str = "local-only-change-me-access-secret"
    refresh_token_secret: str = "local-only-change-me-refresh-secret"
    access_token_key_id: str = "access-v1"
    refresh_token_key_id: str = "refresh-v1"
    access_token_previous_secret: str | None = None
    refresh_token_previous_secret: str | None = None
    access_token_previous_key_id: str | None = None
    refresh_token_previous_key_id: str | None = None
    access_token_previous_valid_until: datetime | None = None
    refresh_token_previous_valid_until: datetime | None = None
    token_key_grace_seconds: int = 3_600
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    agent_token_default_days: int = 30
    initial_admin_email: str = "admin@example.com"
    initial_admin_password: str = "change-me-now"
    allow_public_registration: bool = True
    refresh_cookie_name: str = "tuxnews_refresh"
    cookie_secure: bool = False
    http_timeout_seconds: float = 15.0
    http_max_bytes: int = 5_000_000
    image_max_bytes: int = 2_000_000
    http_max_redirects: int = 5
    http_allowed_ports: list[int] = Field(default_factory=lambda: [80, 443])
    http_allowed_mime_types: list[str] = Field(
        default_factory=lambda: [
            "application/atom+xml",
            "application/json",
            "application/rss+xml",
            "application/xml",
            "image/*",
            "text/html",
            "text/xml",
        ]
    )
    ingestion_max_attempts: int = 5
    ingestion_base_backoff_seconds: float = 5.0
    ingestion_max_backoff_seconds: float = 300.0
    ingestion_poll_interval_seconds: int = 900
    llm_default_profile: Literal["eco", "cloud", "hybrid"] = "eco"
    llm_eco_model: str = "ollama/llama3.2:3b"
    llm_cloud_model: str = "openai/gpt-4o-mini"
    llm_hybrid_model: str = "openai/gpt-4o-mini"
    llm_timeout_seconds: float = 30.0
    llm_max_tokens: int = 512
    llm_temperature: float = 0.0
    llm_max_retries: int = 1
    llm_retry_backoff_seconds: float = 0.25
    llm_input_cost_per_million_usd: float = 0.0
    llm_output_cost_per_million_usd: float = 0.0
    llm_cost_currency: str = "USD"
    llm_usage_retention_days: int = 90
    audit_retention_days: int = 180
    discovery_timeout_seconds: float = 10.0
    discovery_max_results: int = 20
    discovery_max_queries: int = 8
    discovery_max_candidates: int = 100
    discovery_max_retries: int = 2
    discovery_retry_backoff_seconds: float = 0.25
    briefing_max_items: int = 8
    briefing_generation_version: str = "briefing-v1"
    score_weight_version: str = "v1"
    score_semantic_weight: float = 0.6
    score_reputation_weight: float = 0.25
    score_feedback_weight: float = 0.15
    score_words_per_minute: int = 200
    rate_limit_requests: int = 120
    rate_limit_auth_requests: int = 10
    rate_limit_window_seconds: int = 60
    quota_policy_version: str = "quota-v1"
    quota_requests_per_window: int = 120
    quota_scope_requests_per_window: int = 60
    quota_operation_requests_per_window: int = 60
    quota_provider_requests_per_window: int = 60
    quota_window_seconds: int = 60
    quota_daily_cost_usd: float = 10.0
    quota_reservation_ttl_seconds: int = 300
    quota_fail_open: bool = True

    @field_validator("access_token_secret", "refresh_token_secret")
    @classmethod
    def require_production_secrets(cls, value: str, info):
        if info.data.get("environment") == "production" and value.startswith("local-only"):
            raise ValueError("production secrets must be explicitly configured")
        if len(value) < 32:
            raise ValueError("token secrets must be at least 32 characters")
        return value

    @field_validator("observability_hash_salt")
    @classmethod
    def require_observability_salt(cls, value: str, info) -> str:
        if info.data.get("environment") == "production" and value.startswith("local-only"):
            raise ValueError("production observability salt must be explicitly configured")
        if len(value) < 32:
            raise ValueError("observability salt must be at least 32 characters")
        return value

    @field_validator("health_check_timeout_seconds")
    @classmethod
    def validate_health_timeout(cls, value: float) -> float:
        if value <= 0 or value > 30:
            raise ValueError("health check timeout must be between zero and thirty seconds")
        return value

    @field_validator("access_token_previous_secret", "refresh_token_previous_secret", mode="before")
    @classmethod
    def normalize_previous_secret(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("access_token_previous_valid_until", "refresh_token_previous_valid_until", mode="before")
    @classmethod
    def normalize_previous_expiry(cls, value: datetime | str | None) -> datetime | str | None:
        return value or None

    @field_validator(
        "access_token_key_id",
        "refresh_token_key_id",
        "access_token_previous_key_id",
        "refresh_token_previous_key_id",
    )
    @classmethod
    def validate_key_id(cls, value: str | None, info) -> str | None:
        if value == "" and "previous" in info.field_name:
            return None
        if value is None:
            return value
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value):
            raise ValueError("key IDs must contain only letters, numbers, dot, underscore, or hyphen")
        return value

    @field_validator("access_token_previous_secret", "refresh_token_previous_secret")
    @classmethod
    def validate_previous_secret(cls, value: str | None, info) -> str | None:
        if value is None:
            return value
        if info.data.get("environment") == "production" and value.startswith("local-only"):
            raise ValueError("previous production secrets must be explicitly configured")
        if len(value) < 32:
            raise ValueError("previous token secrets must be at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_keyring(self) -> "Settings":
        if self.environment == "production" and self.allow_public_registration:
            raise ValueError("public registration must be disabled in production")
        if self.environment == "production" and self.quota_fail_open:
            raise ValueError("production quota enforcement cannot fail open")
        for prefix in ("access_token", "refresh_token"):
            previous_secret = getattr(self, f"{prefix}_previous_secret")
            previous_key_id = getattr(self, f"{prefix}_previous_key_id")
            previous_valid_until = getattr(self, f"{prefix}_previous_valid_until")
            if previous_secret is None and (previous_key_id is not None or previous_valid_until is not None):
                raise ValueError(f"{prefix} previous key metadata requires a previous secret")
            if previous_secret is not None and (previous_key_id is None or previous_valid_until is None):
                raise ValueError(f"{prefix} previous secret requires key ID and validity window")
            if previous_key_id == getattr(self, f"{prefix}_key_id"):
                raise ValueError(f"{prefix} active and previous key IDs must differ")
        if self.token_key_grace_seconds < 1:
            raise ValueError("token key grace period must be positive")
        return self

    @field_validator("http_allowed_ports")
    @classmethod
    def validate_http_ports(cls, value: list[int]) -> list[int]:
        if any(port < 1 or port > 65_535 for port in value):
            raise ValueError("HTTP ports must be between 1 and 65535")
        return value

    @field_validator("llm_input_cost_per_million_usd", "llm_output_cost_per_million_usd")
    @classmethod
    def validate_llm_costs(cls, value: float) -> float:
        if value < 0:
            raise ValueError("LLM cost rates cannot be negative")
        return value

    @field_validator("llm_cost_currency")
    @classmethod
    def validate_llm_cost_currency(cls, value: str) -> str:
        if value.upper() != "USD":
            raise ValueError("LLM usage costs must be normalized to USD")
        return "USD"

    @field_validator("llm_usage_retention_days")
    @classmethod
    def validate_usage_retention(cls, value: int) -> int:
        if value < 1:
            raise ValueError("LLM usage retention must be at least one day")
        return value

    @field_validator("audit_retention_days")
    @classmethod
    def validate_audit_retention(cls, value: int) -> int:
        if value < 1:
            raise ValueError("audit retention must be at least one day")
        return value

    @field_validator(
        "quota_requests_per_window",
        "quota_scope_requests_per_window",
        "quota_operation_requests_per_window",
        "quota_provider_requests_per_window",
        "quota_window_seconds",
        "quota_reservation_ttl_seconds",
    )
    @classmethod
    def validate_quota_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("quota limits and windows must be positive")
        return value

    @field_validator("quota_daily_cost_usd")
    @classmethod
    def validate_quota_cost(cls, value: float) -> float:
        if value < 0:
            raise ValueError("daily quota cost cannot be negative")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
