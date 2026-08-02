from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.core.config import Settings
from app.core.keyring import SigningKey, SigningKeyRing
from app.core.security import create_access_token, decode_token


def _settings(
    *,
    access_secret: str,
    access_key_id: str,
    refresh_secret: str,
    refresh_key_id: str,
    previous_access_secret: str | None = None,
    previous_access_key_id: str | None = None,
    previous_access_until: datetime | None = None,
) -> Settings:
    return Settings(
        access_token_secret=access_secret,
        access_token_key_id=access_key_id,
        refresh_token_secret=refresh_secret,
        refresh_token_key_id=refresh_key_id,
        access_token_previous_secret=previous_access_secret,
        access_token_previous_key_id=previous_access_key_id,
        access_token_previous_valid_until=previous_access_until,
    )


def test_keyring_only_accepts_previous_key_inside_window() -> None:
    now = datetime.now(UTC)
    previous = SigningKey("v1", "old-secret")
    ring = SigningKeyRing(
        active=SigningKey("v2", "new-secret"),
        previous=previous,
        previous_valid_until=now + timedelta(minutes=5),
    )
    assert [key.key_id for key in ring.verification_keys(now=now)] == ["v2", "v1"]
    assert [key.key_id for key in ring.verification_keys(now=now + timedelta(minutes=6))] == ["v2"]
    assert "old-secret" not in repr(previous)


def test_tokens_rotate_with_kid_and_rollback_without_downtime(monkeypatch) -> None:
    now = datetime.now(UTC)
    old = _settings(
        access_secret="a" * 40,
        access_key_id="access-v1",
        refresh_secret="r" * 40,
        refresh_key_id="refresh-v1",
    )
    rotated = _settings(
        access_secret="b" * 40,
        access_key_id="access-v2",
        refresh_secret="s" * 40,
        refresh_key_id="refresh-v2",
        previous_access_secret=old.access_token_secret,
        previous_access_key_id=old.access_token_key_id,
        previous_access_until=now + timedelta(hours=1),
    )
    monkeypatch.setattr("app.core.security.get_settings", lambda: old)
    old_token = create_access_token(7, scopes=["content:read"])
    assert jwt.get_unverified_header(old_token)["kid"] == "access-v1"

    monkeypatch.setattr("app.core.security.get_settings", lambda: rotated)
    new_token = create_access_token(7, scopes=["content:read"])
    assert jwt.get_unverified_header(new_token)["kid"] == "access-v2"
    assert decode_token(old_token, "access")["sub"] == "7"
    assert decode_token(new_token, "access")["sub"] == "7"

    expired = rotated.model_copy(update={"access_token_previous_valid_until": now - timedelta(seconds=1)})
    monkeypatch.setattr("app.core.security.get_settings", lambda: expired)
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(old_token, "access")

    rollback = _settings(
        access_secret=old.access_token_secret,
        access_key_id=old.access_token_key_id,
        refresh_secret=old.refresh_token_secret,
        refresh_key_id=old.refresh_token_key_id,
        previous_access_secret=rotated.access_token_secret,
        previous_access_key_id=rotated.access_token_key_id,
        previous_access_until=now + timedelta(hours=1),
    )
    monkeypatch.setattr("app.core.security.get_settings", lambda: rollback)
    assert decode_token(new_token, "access")["sub"] == "7"
