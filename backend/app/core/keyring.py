from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class SigningKey:
    key_id: str
    secret: str = field(repr=False)


@dataclass(frozen=True)
class SigningKeyRing:
    active: SigningKey
    previous: SigningKey | None = None
    previous_valid_until: datetime | None = None

    def verification_keys(self, *, now: datetime | None = None) -> tuple[SigningKey, ...]:
        current_time = now or datetime.now(UTC)
        if self.previous is None or self.previous_valid_until is None:
            return (self.active,)
        valid_until = self.previous_valid_until
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=UTC)
        if current_time.astimezone(UTC) >= valid_until.astimezone(UTC):
            return (self.active,)
        return self.active, self.previous
