from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.archive.paths import ArchivePathError, confined_path, tenant_relative_path
from app.audit.service import anonymize_audit_for_user, record_audit
from app.core.config import get_settings
from app.core.security import hash_one_time_token, hash_password, new_one_time_token
from app.db.models import (
    AgentToken,
    ArchiveExport,
    Article,
    Briefing,
    BriefingItem,
    BriefingSchedule,
    Cluster,
    ClusterMember,
    DiscoveryRun,
    Feedback,
    IngestionRun,
    Invitation,
    Source,
    UsageEvent,
    User,
    UserActionToken,
    UserSession,
    UserTopic,
)
from app.embeddings.qdrant_index import EmbeddingIndex
from app.observability import log_event
from app.usage.service import enable_usage_maintenance

logger = logging.getLogger(__name__)

ACTION_TOKEN_TTL = timedelta(hours=1)
MAX_INVITATION_TTL = timedelta(days=7)
ALLOWED_ROLES = frozenset({"admin", "user"})


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_role(role: str) -> str:
    if role not in ALLOWED_ROLES:
        raise ValueError("invalid user role")
    return role


def invitation_expiry(expires_in_hours: int) -> datetime:
    if expires_in_hours < 1 or timedelta(hours=expires_in_hours) > MAX_INVITATION_TTL:
        raise ValueError("invitation expiry must be between 1 and 168 hours")
    return datetime.now(UTC) + timedelta(hours=expires_in_hours)


async def create_invitation(
    session: AsyncSession,
    *,
    invited_by_user_id: int,
    email: str,
    role: str,
    expires_in_hours: int,
    correlation_id: str | None = None,
) -> tuple[Invitation, str]:
    normalized_email = normalize_email(email)
    normalized_role = validate_role(role)
    existing = await session.scalar(select(User).where(User.email == normalized_email))
    if existing is not None:
        raise ValueError("email already registered")

    now = datetime.now(UTC)
    await session.execute(
        update(Invitation)
        .where(
            Invitation.email == normalized_email,
            Invitation.used_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    raw_token = new_one_time_token("tn_invite")
    invitation = Invitation(
        email=normalized_email,
        role=normalized_role,
        token_hash=hash_one_time_token(raw_token),
        invited_by_user_id=invited_by_user_id,
        expires_at=invitation_expiry(expires_in_hours),
    )
    session.add(invitation)
    await session.flush()
    record_audit(
        session,
        user_id=invited_by_user_id,
        action="user.invitation_created",
        resource_type="invitation",
        resource_id=str(invitation.id),
        outcome="success",
        correlation_id=correlation_id,
        actor_type="admin",
        actor_id=str(invited_by_user_id),
        details={"role": normalized_role, "expires_at": invitation.expires_at.isoformat()},
    )
    await session.commit()
    await session.refresh(invitation)
    return invitation, raw_token


async def accept_invitation(
    session: AsyncSession,
    *,
    token: str,
    password: str,
    correlation_id: str | None = None,
) -> User | None:
    invitation = await session.scalar(
        select(Invitation).where(Invitation.token_hash == hash_one_time_token(token)).with_for_update()
    )
    now = datetime.now(UTC)
    if (
        invitation is None
        or invitation.used_at is not None
        or invitation.revoked_at is not None
        or _as_utc(invitation.expires_at) <= now
    ):
        return None

    existing = await session.scalar(select(User).where(User.email == invitation.email))
    invitation.used_at = now
    if existing is not None:
        await session.commit()
        return None

    user = User(
        email=invitation.email,
        password_hash=hash_password(password),
        role=validate_role(invitation.role),
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return None
    record_audit(
        session,
        user_id=user.id,
        action="user.invitation_accepted",
        resource_type="user",
        resource_id=str(user.id),
        outcome="success",
        correlation_id=correlation_id,
        actor_type="user",
        actor_id=str(user.id),
        details={"invitation_id": invitation.id},
    )
    await session.commit()
    await session.refresh(user)
    return user


async def list_users(session: AsyncSession) -> list[User]:
    result = await session.scalars(
        select(User)
        .where(User.deleted_at.is_(None))
        .order_by(User.created_at.asc(), User.id.asc())
    )
    return list(result)


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.scalar(select(User).where(User.id == user_id, User.deleted_at.is_(None)))


async def _load_user_for_update(session: AsyncSession, user_id: int) -> User | None:
    return await session.scalar(
        select(User).where(User.id == user_id, User.deleted_at.is_(None)).with_for_update()
    )


async def _ensure_admin_can_change(
    session: AsyncSession,
    *,
    actor_id: int,
    target: User,
    changing_role: str | None = None,
    changing_active: bool | None = None,
    deleting: bool = False,
) -> None:
    if actor_id == target.id:
        raise ValueError("administrators cannot modify their own account")
    removes_admin = (
        target.role == "admin"
        and target.is_active
        and (changing_role == "user" or changing_active is False or deleting)
    )
    if not removes_admin:
        return
    active_admins = await session.scalar(
        select(func.count(User.id)).where(User.role == "admin", User.is_active.is_(True), User.deleted_at.is_(None))
    )
    if active_admins is not None and active_admins <= 1:
        raise ValueError("at least one active administrator is required")


async def _revoke_credentials(session: AsyncSession, user_id: int) -> None:
    now = datetime.now(UTC)
    await session.execute(
        update(User).where(User.id == user_id).values(tokens_revoked_at=now)
    )
    await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.execute(
        update(AgentToken).where(AgentToken.user_id == user_id, AgentToken.revoked_at.is_(None)).values(revoked_at=now)
    )


async def update_user(
    session: AsyncSession,
    *,
    actor_id: int,
    user_id: int,
    role: str | None = None,
    is_active: bool | None = None,
    correlation_id: str | None = None,
) -> User | None:
    target = await _load_user_for_update(session, user_id)
    if target is None:
        return None
    normalized_role = validate_role(role) if role is not None else None
    await _ensure_admin_can_change(
        session,
        actor_id=actor_id,
        target=target,
        changing_role=normalized_role,
        changing_active=is_active,
    )
    if normalized_role is not None:
        target.role = normalized_role
    if is_active is False:
        target.is_active = False
        target.suspended_at = datetime.now(UTC)
        await _revoke_credentials(session, target.id)
    elif is_active is True:
        target.is_active = True
        target.suspended_at = None
    if normalized_role is None and is_active is None:
        return target
    record_audit(
        session,
        user_id=target.id,
        action="user.updated",
        resource_type="user",
        resource_id=str(target.id),
        outcome="success",
        correlation_id=correlation_id,
        actor_type="admin",
        actor_id=str(actor_id),
        details={"role": target.role, "is_active": target.is_active},
    )
    await session.commit()
    await session.refresh(target)
    return target


async def suspend_user(
    session: AsyncSession,
    *,
    actor_id: int,
    user_id: int,
    correlation_id: str | None = None,
) -> User | None:
    return await update_user(
        session,
        actor_id=actor_id,
        user_id=user_id,
        is_active=False,
        correlation_id=correlation_id,
    )


async def reactivate_user(
    session: AsyncSession,
    *,
    actor_id: int,
    user_id: int,
    correlation_id: str | None = None,
) -> User | None:
    return await update_user(
        session,
        actor_id=actor_id,
        user_id=user_id,
        is_active=True,
        correlation_id=correlation_id,
    )


def _remove_archive_files(user_id: int, paths: Sequence[str]) -> None:
    root = Path(get_settings().archive_root)
    for relative_path in paths:
        try:
            tenant_path = tenant_relative_path(user_id, Path(relative_path))
            destination = confined_path(root, *tenant_path.parts)
            if destination.is_file() and not destination.is_symlink():
                destination.unlink()
        except (ArchivePathError, OSError):
            log_event(
                logger,
                "user.archive_cleanup_skipped",
                level=logging.WARNING,
                resource_type="user_archive",
                error_type="cleanup_error",
            )


async def _cleanup_user_vectors(user_id: int) -> None:
    index: EmbeddingIndex | None = None
    try:
        index = EmbeddingIndex()
        timeout = min(get_settings().http_timeout_seconds, 5.0)
        await asyncio.wait_for(index.delete_user(user_id=user_id), timeout=timeout)
    except Exception as exc:
        log_event(
            logger,
            "user.vector_cleanup_deferred",
            level=logging.WARNING,
            user_id=user_id,
            error_type=type(exc).__name__,
        )
    finally:
        if index is not None:
            await index.aclose()


async def delete_user(
    session: AsyncSession,
    *,
    actor_id: int,
    user_id: int,
    correlation_id: str | None = None,
) -> bool:
    target = await _load_user_for_update(session, user_id)
    if target is None:
        return False
    await _ensure_admin_can_change(session, actor_id=actor_id, target=target, deleting=True)
    archive_paths = list(
        await session.scalars(select(ArchiveExport.path).where(ArchiveExport.user_id == target.id))
    )
    article_ids = select(Article.id).where(Article.user_id == target.id)
    briefing_ids = select(Briefing.id).where(Briefing.user_id == target.id)
    cluster_ids = select(Cluster.id).where(Cluster.user_id == target.id)

    await session.execute(
        delete(BriefingItem).where(
            or_(
                BriefingItem.article_id.in_(article_ids),
                BriefingItem.briefing_id.in_(briefing_ids),
            ),
            BriefingItem.user_id == target.id,
        )
    )
    await session.execute(
        delete(ClusterMember).where(
            or_(
                ClusterMember.article_id.in_(article_ids),
                ClusterMember.cluster_id.in_(cluster_ids),
            ),
            ClusterMember.user_id == target.id,
        )
    )
    await enable_usage_maintenance(session)
    for model in (
        ArchiveExport,
        Feedback,
        IngestionRun,
        DiscoveryRun,
        Briefing,
        BriefingSchedule,
        UserTopic,
        UsageEvent,
        AgentToken,
        UserSession,
        UserActionToken,
    ):
        await session.execute(delete(model).where(model.user_id == target.id))
    await session.execute(delete(Article).where(Article.user_id == target.id))
    await session.execute(delete(Cluster).where(Cluster.user_id == target.id))
    await session.execute(delete(Source).where(Source.user_id == target.id))
    await session.execute(
        update(Invitation)
        .where(Invitation.invited_by_user_id == target.id)
        .values(invited_by_user_id=None)
    )
    await anonymize_audit_for_user(session, user_id=target.id)
    record_audit(
        session,
        user_id=None,
        action="user.deleted",
        resource_type="user",
        resource_id=str(target.id),
        outcome="success",
        correlation_id=correlation_id,
        actor_type="admin",
        actor_id=str(actor_id),
        tenant_id=target.id,
        details={"target_user_id": target.id, "cleanup": "sql_and_archive"},
    )
    await session.delete(target)
    await session.commit()
    _remove_archive_files(target.id, archive_paths)
    await _cleanup_user_vectors(target.id)
    return True


async def _issue_action_token(
    session: AsyncSession,
    *,
    user: User,
    purpose: str,
    target_email: str | None = None,
    correlation_id: str | None = None,
) -> str:
    now = datetime.now(UTC)
    await session.execute(
        update(UserActionToken)
        .where(
            UserActionToken.user_id == user.id,
            UserActionToken.purpose == purpose,
            UserActionToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    raw_token = new_one_time_token("tn_action")
    action_token = UserActionToken(
        user_id=user.id,
        purpose=purpose,
        token_hash=hash_one_time_token(raw_token),
        target_email=target_email,
        expires_at=now + ACTION_TOKEN_TTL,
    )
    session.add(action_token)
    await session.flush()
    record_audit(
        session,
        user_id=user.id,
        action=f"account.{purpose}.requested",
        resource_type="user_action_token",
        resource_id=str(action_token.id),
        outcome="success",
        correlation_id=correlation_id,
        details={"target_email_changed": target_email is not None},
    )
    await session.commit()
    return raw_token


async def issue_password_recovery(
    session: AsyncSession,
    *,
    user_id: int,
    correlation_id: str | None = None,
) -> str | None:
    user = await session.scalar(
        select(User).where(User.id == user_id, User.is_active.is_(True), User.deleted_at.is_(None))
    )
    if user is None:
        return None
    return await _issue_action_token(
        session,
        user=user,
        purpose="password_recovery",
        correlation_id=correlation_id,
    )


async def issue_email_change(
    session: AsyncSession,
    *,
    user_id: int,
    new_email: str,
    correlation_id: str | None = None,
) -> str | None:
    user = await session.scalar(
        select(User).where(User.id == user_id, User.is_active.is_(True), User.deleted_at.is_(None))
    )
    if user is None:
        return None
    normalized_email = normalize_email(new_email)
    existing = await session.scalar(select(User).where(User.email == normalized_email, User.id != user_id))
    if existing is not None:
        raise ValueError("email already registered")
    return await _issue_action_token(
        session,
        user=user,
        purpose="email_change",
        target_email=normalized_email,
        correlation_id=correlation_id,
    )


async def _get_action_token(
    session: AsyncSession,
    *,
    token: str,
    purpose: str,
) -> UserActionToken | None:
    action_token = await session.scalar(
        select(UserActionToken)
        .where(
            UserActionToken.token_hash == hash_one_time_token(token),
            UserActionToken.purpose == purpose,
        )
        .with_for_update()
    )
    if (
        action_token is None
        or action_token.used_at is not None
        or _as_utc(action_token.expires_at) <= datetime.now(UTC)
    ):
        return None
    return action_token


async def complete_password_recovery(
    session: AsyncSession,
    *,
    token: str,
    new_password: str,
    correlation_id: str | None = None,
) -> bool:
    action_token = await _get_action_token(session, token=token, purpose="password_recovery")
    if action_token is None:
        return False
    user = await session.scalar(
        select(User).where(User.id == action_token.user_id, User.is_active.is_(True), User.deleted_at.is_(None))
    )
    if user is None:
        return False
    user.password_hash = hash_password(new_password)
    action_token.used_at = datetime.now(UTC)
    await _revoke_credentials(session, user.id)
    record_audit(
        session,
        user_id=user.id,
        action="account.password_recovery.completed",
        resource_type="user",
        resource_id=str(user.id),
        outcome="success",
        correlation_id=correlation_id,
    )
    await session.commit()
    return True


async def complete_email_change(
    session: AsyncSession,
    *,
    token: str,
    correlation_id: str | None = None,
) -> bool:
    action_token = await _get_action_token(session, token=token, purpose="email_change")
    if action_token is None or action_token.target_email is None:
        return False
    user = await session.scalar(
        select(User).where(User.id == action_token.user_id, User.is_active.is_(True), User.deleted_at.is_(None))
    )
    if user is None:
        return False
    existing = await session.scalar(
        select(User).where(User.email == action_token.target_email, User.id != user.id)
    )
    if existing is not None:
        return False
    user.email = action_token.target_email
    action_token.used_at = datetime.now(UTC)
    await _revoke_credentials(session, user.id)
    record_audit(
        session,
        user_id=user.id,
        action="account.email_change.completed",
        resource_type="user",
        resource_id=str(user.id),
        outcome="success",
        correlation_id=correlation_id,
    )
    await session.commit()
    return True
