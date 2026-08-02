from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tokens_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    serendipity_score: Mapped[float] = mapped_column(Float, default=0.25, nullable=False)
    ranking_preference_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class UserSession(TimestampMixin, Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    family_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentToken(TimestampMixin, Base):
    __tablename__ = "agent_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invitation(TimestampMixin, Base):
    __tablename__ = "user_invitations"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'admin')", name="ck_user_invitations_role"),
        Index("ix_user_invitations_email", "email"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    invited_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserActionToken(TimestampMixin, Base):
    __tablename__ = "user_action_tokens"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('password_recovery', 'email_change')",
            name="ck_user_action_tokens_purpose",
        ),
        Index("ix_user_action_tokens_user_purpose", "user_id", "purpose"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    target_email: Mapped[str | None] = mapped_column(String(320))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Source(TimestampMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("user_id", "url", name="uq_sources_user_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), default="rss", nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), default="dynamic", nullable=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reputation_score: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    reputation_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_muted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    preference_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Article(TimestampMixin, Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("user_id", "canonical_url_hash", name="uq_articles_user_url_hash"),
        CheckConstraint(
            "status IN ('discovered', 'fetching', 'extracted', 'curated', 'indexed', 'published', 'failed')",
            name="ck_articles_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    canonical_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    author: Mapped[str | None] = mapped_column(String(300))
    content_clean: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    read_time_minutes: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(24), default="discovered", index=True, nullable=False)
    status_error: Mapped[str | None] = mapped_column(String(1000))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    fetch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    curated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_stage_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("clusters.id", ondelete="SET NULL"))
    image_local_path: Mapped[str | None] = mapped_column(String(2048))
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0, index=True, nullable=False)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    feedback_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    embedding_version: Mapped[str | None] = mapped_column(String(64))


class Cluster(TimestampMixin, Base):
    __tablename__ = "clusters"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'stale', 'empty', 'ambiguous')", name="ck_clusters_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    algorithm_version: Mapped[str] = mapped_column(String(64), default="cosine-v1", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True, nullable=False)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClusterMember(TimestampMixin, Base):
    __tablename__ = "cluster_members"
    __table_args__ = (
        UniqueConstraint("cluster_id", "article_id", "algorithm_version", name="uq_cluster_member_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), index=True)
    similarity_score: Mapped[float] = mapped_column(Float, nullable=False)
    membership_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)


class Feedback(TimestampMixin, Base):
    __tablename__ = "feedbacks"
    __table_args__ = (
        CheckConstraint(
            "(action_type IN ('article', 'quality') AND article_id IS NOT NULL AND "
            "source_id IS NULL AND topic_name IS NULL) OR "
            "(action_type = 'source' AND article_id IS NULL AND source_id IS NOT NULL AND "
            "topic_name IS NULL) OR "
            "(action_type = 'topic' AND article_id IS NULL AND source_id IS NULL AND "
            "topic_name IS NOT NULL)",
            name="ck_feedback_target",
        ),
        Index(
            "uq_feedback_current_article",
            "user_id",
            "article_id",
            "action_type",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
        Index(
            "uq_feedback_current_source",
            "user_id",
            "source_id",
            "action_type",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
        Index(
            "uq_feedback_current_topic",
            "user_id",
            "topic_name",
            "action_type",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    topic_name: Mapped[str | None] = mapped_column(String(200), index=True)
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    action_type: Mapped[str] = mapped_column(String(16), default="article", nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("feedbacks.id", ondelete="SET NULL"), index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)


class UserTopic(TimestampMixin, Base):
    __tablename__ = "user_topics"
    __table_args__ = (UniqueConstraint("user_id", "topic_name", name="uq_user_topics_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    topic_name: Mapped[str] = mapped_column(String(200), nullable=False)
    weight_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    preference_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    vector_representation: Mapped[list[float] | None] = mapped_column(JSON)


class BriefingSchedule(TimestampMixin, Base):
    __tablename__ = "briefing_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    local_time: Mapped[str] = mapped_column(String(5), default="08:00", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Briefing(TimestampMixin, Base):
    __tablename__ = "briefings"
    __table_args__ = (UniqueConstraint("user_id", "briefing_date", "local_time", name="uq_briefing_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    briefing_date: Mapped[str] = mapped_column(String(10), nullable=False)
    local_time: Mapped[str] = mapped_column(String(5), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ready", nullable=False)
    security_context: Mapped[str] = mapped_column(String(64), default="UNTRUSTED_EXTERNAL_DATA", nullable=False)
    generation_version: Mapped[str] = mapped_column(String(64), default="briefing-v1", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(1000))


class BriefingItem(TimestampMixin, Base):
    __tablename__ = "briefing_items"
    __table_args__ = (
        UniqueConstraint("briefing_id", "article_id", name="uq_briefing_items_article"),
        CheckConstraint("article_id IS NOT NULL", name="ck_briefing_items_article_required"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    briefing_id: Mapped[int] = mapped_column(ForeignKey("briefings.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    display_rank: Mapped[float] = mapped_column(Float, nullable=False)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ArchiveExport(TimestampMixin, Base):
    __tablename__ = "archive_exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(2048), nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="ready", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(500))


class IngestionRun(TimestampMixin, Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000))


class DiscoveryRun(TimestampMixin, Base):
    __tablename__ = "discovery_runs"
    __table_args__ = (UniqueConstraint("user_id", "slot_key", name="uq_discovery_runs_user_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    slot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_version: Mapped[str] = mapped_column(String(80), nullable=False)
    serendipity_score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    query_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(2000))


class UsageEvent(TimestampMixin, Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_tenant_created_at", "tenant_id", "created_at"),
        Index("ix_usage_events_provider_model_created_at", "provider", "model", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    actor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_is_estimated: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cost_currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), default="success", nullable=False)
    used_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120))
    provider_request_id: Mapped[str | None] = mapped_column(String(160))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)


class AuditEvent(TimestampMixin, Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, index=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, index=True)
    actor_type: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(120))
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
