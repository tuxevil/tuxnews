from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import Settings, get_settings


class InvalidCursor(ValueError):
    """Raised when a feed cursor is malformed or signed with another secret."""


@dataclass(frozen=True)
class FeedCursor:
    score: float
    published_at: datetime
    article_id: int


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _encode_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_part(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def encode_cursor(cursor: FeedCursor, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    payload = json.dumps(
        {"s": round(cursor.score, 8), "p": _as_utc(cursor.published_at).isoformat(), "i": cursor.article_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = _encode_part(payload)
    signature = hmac.new(settings.access_token_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_encode_part(signature)}"


def decode_cursor(value: str, settings: Settings | None = None) -> FeedCursor:
    settings = settings or get_settings()
    try:
        encoded, encoded_signature = value.split(".", 1)
        expected = hmac.new(
            settings.access_token_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _decode_part(encoded_signature)):
            raise InvalidCursor("invalid cursor")
        payload = json.loads(_decode_part(encoded))
        score = float(payload["s"])
        published_at = datetime.fromisoformat(payload["p"]).astimezone(UTC)
        article_id = int(payload["i"])
        if not (-1 <= score <= 1) or article_id < 1:
            raise InvalidCursor("invalid cursor")
        return FeedCursor(score=score, published_at=published_at, article_id=article_id)
    except (InvalidCursor, KeyError, TypeError, ValueError, json.JSONDecodeError, binascii.Error) as exc:
        raise InvalidCursor("invalid cursor") from exc
