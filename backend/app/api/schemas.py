from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    role: str


class UserAdminPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    role: Literal["admin", "user"]
    is_active: bool
    suspended_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminUserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None


class InvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    role: Literal["admin", "user"] = "user"
    expires_in_hours: int = Field(default=72, ge=1, le=168)


class InvitationCreated(BaseModel):
    id: int
    role: Literal["admin", "user"]
    expires_at: datetime
    token: str


class InvitationAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=256)
    password: str = Field(min_length=12, max_length=128)


class PasswordRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class PasswordRecoveryConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=256)
    new_password: str = Field(min_length=12, max_length=128)


class EmailChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_email: EmailStr
    current_password: str = Field(min_length=1, max_length=128)


class EmailChangeConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=256)


class AccountActionResponse(BaseModel):
    status: str = "accepted"


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    scopes: list[str]
    user: UserPublic


class LogoutResponse(BaseModel):
    status: str = "ok"


class SessionInfo(BaseModel):
    id: int
    expires_at: datetime


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    url: HttpUrl
    source_type: Literal["rss", "atom"] = "rss"
    tags: list[str] = Field(default_factory=list, max_length=32)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be empty")
        return normalized

    @field_validator("url")
    @classmethod
    def reject_url_credentials(cls, value: HttpUrl) -> HttpUrl:
        parsed = urlsplit(str(value))
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("source URL credentials are not allowed")
        return value


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: HttpUrl | None = None
    source_type: Literal["rss", "atom"] | None = None
    tags: list[str] | None = Field(default=None, max_length=32)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("name cannot be empty")
        return normalized

    @field_validator("url")
    @classmethod
    def reject_url_credentials(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None:
            parsed = urlsplit(str(value))
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("source URL credentials are not allowed")
        return value


class SourcePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: HttpUrl
    source_type: str
    tags: list[str]
    is_active: bool
    origin: str
    is_muted: bool


class FeedItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int = 0
    source_name: str = "Unknown source"
    title: str
    original_title: str
    url: HttpUrl
    author: str | None
    summary: str | None
    tags: list[str]
    read_time_minutes: int | None
    published_at: datetime | None
    status: str
    relevance_score: float
    score_breakdown: dict[str, float]
    security_context: str = "UNTRUSTED_EXTERNAL_DATA"
    display_rank: float = 0.0
    cluster_id: int | None = None


class FeedResponse(BaseModel):
    items: list[FeedItem]
    next_cursor: str | None = None
