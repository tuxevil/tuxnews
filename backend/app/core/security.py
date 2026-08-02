import hashlib
import secrets
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings
from app.core.keyring import SigningKey, SigningKeyRing

password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_agent_token() -> str:
    return f"tn_agent_{secrets.token_urlsafe(32)}"


def new_one_time_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_one_time_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_is_revoked(payload: dict[str, Any], revoked_at: datetime | None) -> bool:
    if revoked_at is None:
        return False
    issued_at = payload.get("issued_at", payload.get("iat"))
    if not isinstance(issued_at, (int, float)):
        return True
    try:
        issued_time = datetime.fromtimestamp(issued_at, UTC)
    except (OverflowError, OSError, ValueError):
        return True
    current_revocation = revoked_at.replace(tzinfo=UTC) if revoked_at.tzinfo is None else revoked_at.astimezone(UTC)
    return issued_time <= current_revocation


def new_family_id() -> str:
    return secrets.token_urlsafe(24)


def _create_token(
    *,
    subject: int,
    token_type: str,
    expires_delta: timedelta,
    family_id: str | None = None,
    scopes: Collection[str] = (),
) -> str:
    settings = get_settings()
    keyring = _keyring(settings, token_type)
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type,
        "iat": now,
        "issued_at": now.timestamp(),
        "exp": now + expires_delta,
        "jti": secrets.token_urlsafe(18),
        "scopes": sorted(set(scopes)),
    }
    if family_id is not None:
        payload["family_id"] = family_id
    return jwt.encode(
        payload,
        keyring.active.secret,
        algorithm="HS256",
        headers={"kid": keyring.active.key_id},
    )


def create_access_token(subject: int, scopes: Collection[str] = ()) -> str:
    settings = get_settings()
    return _create_token(
        subject=subject,
        token_type="access",
        expires_delta=timedelta(minutes=settings.access_token_minutes),
        scopes=scopes,
    )


def create_refresh_token(subject: int, family_id: str) -> str:
    settings = get_settings()
    return _create_token(
        subject=subject,
        token_type="refresh",
        family_id=family_id,
        expires_delta=timedelta(days=settings.refresh_token_days),
    )


def decode_token(token: str, expected_type: str) -> dict[str, Any]:
    settings = get_settings()
    keyring = _keyring(settings, expected_type)
    try:
        key_id = jwt.get_unverified_header(token).get("kid")
    except jwt.InvalidTokenError as exc:
        raise jwt.InvalidTokenError("invalid token header") from exc
    candidates = keyring.verification_keys()
    if key_id is not None:
        candidates = tuple(key for key in candidates if key.key_id == key_id)
    if not candidates:
        raise jwt.InvalidTokenError("unknown signing key")
    last_error: jwt.InvalidTokenError | None = None
    for key in candidates:
        try:
            payload = jwt.decode(token, key.secret, algorithms=["HS256"])
        except jwt.InvalidTokenError as exc:
            last_error = exc
            continue
        if payload.get("type") != expected_type or not payload.get("sub"):
            raise jwt.InvalidTokenError("invalid token type")
        return payload
    if last_error is not None:
        raise last_error
    raise jwt.InvalidTokenError("invalid token")


def _keyring(settings: Any, token_type: str) -> SigningKeyRing:
    if token_type == "access":
        return SigningKeyRing(
            active=SigningKey(settings.access_token_key_id, settings.access_token_secret),
            previous=(
                SigningKey(settings.access_token_previous_key_id, settings.access_token_previous_secret)
                if settings.access_token_previous_key_id and settings.access_token_previous_secret
                else None
            ),
            previous_valid_until=settings.access_token_previous_valid_until,
        )
    if token_type == "refresh":
        return SigningKeyRing(
            active=SigningKey(settings.refresh_token_key_id, settings.refresh_token_secret),
            previous=(
                SigningKey(settings.refresh_token_previous_key_id, settings.refresh_token_previous_secret)
                if settings.refresh_token_previous_key_id and settings.refresh_token_previous_secret
                else None
            ),
            previous_valid_until=settings.refresh_token_previous_valid_until,
        )
    raise ValueError("unknown token type")
