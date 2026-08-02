from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_refresh_token,
    new_family_id,
    verify_password,
)


def test_password_hash_is_one_way_and_verifiable() -> None:
    password_hash = hash_password("correct horse battery staple")
    assert password_hash != "correct horse battery staple"
    assert verify_password("correct horse battery staple", password_hash)
    assert not verify_password("wrong password", password_hash)


def test_access_and_refresh_tokens_are_scoped() -> None:
    access = create_access_token(7)
    family_id = new_family_id()
    refresh = create_refresh_token(7, family_id)
    assert decode_token(access, "access")["sub"] == "7"
    assert decode_token(refresh, "refresh")["family_id"] == family_id

    try:
        decode_token(access, "refresh")
    except jwt.InvalidTokenError:
        pass
    else:
        raise AssertionError("access token accepted as refresh token")


def test_refresh_hash_is_stable_and_non_reversible() -> None:
    token = "opaque-token"
    digest = hash_refresh_token(token)
    assert digest == hash_refresh_token(token)
    assert digest != token


def test_expired_access_token_is_rejected() -> None:
    settings = get_settings()
    token = jwt.encode(
        {
            "sub": "7",
            "type": "access",
            "iat": datetime.now(UTC) - timedelta(minutes=2),
            "exp": datetime.now(UTC) - timedelta(minutes=1),
        },
        settings.access_token_secret,
        algorithm="HS256",
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(token, "access")
