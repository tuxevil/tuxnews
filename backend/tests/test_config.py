import pytest
from app.core.config import Settings
from pydantic import ValidationError


def test_local_settings_have_safe_lengths() -> None:
    settings = Settings()
    assert len(settings.access_token_secret) >= 32
    assert len(settings.refresh_token_secret) >= 32


def test_production_rejects_placeholder_secrets() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production")


def test_production_accepts_explicit_secrets() -> None:
    settings = Settings(
        environment="production",
        access_token_secret="a" * 40,
        refresh_token_secret="b" * 40,
        observability_hash_salt="c" * 40,
        allow_public_registration=False,
        quota_fail_open=False,
    )
    assert settings.environment == "production"
