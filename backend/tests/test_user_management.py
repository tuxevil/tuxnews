from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from app.agent_tokens.service import create_agent_token
from app.core.security import create_access_token, hash_password
from app.db.models import AuditEvent, Invitation, Source, User, UserActionToken, UserSession
from app.mcp.auth import TuxnewsTokenVerifier
from app.users.service import (
    complete_email_change,
    complete_password_recovery,
    issue_email_change,
    issue_password_recovery,
)
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def _admin_headers(auth_client: AsyncClient, db_session: AsyncSession) -> dict[str, str]:
    admin = User(
        email="management-admin@example.com",
        password_hash=hash_password("admin-password-123"),
        role="admin",
    )
    db_session.add(admin)
    await db_session.commit()
    response = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": admin.email, "password": "admin-password-123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_only_admins_can_invite_and_invitation_is_single_use(
    auth_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    regular = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "regular@example.com", "password": "correct horse battery staple"},
    )
    assert regular.status_code == 201
    regular_headers = {"Authorization": f"Bearer {regular.json()['access_token']}"}

    forbidden = await auth_client.get("/api/v1/admin/users", headers=regular_headers)
    assert forbidden.status_code == 403

    admin_headers = await _admin_headers(auth_client, db_session)
    invitation = await auth_client.post(
        "/api/v1/admin/invitations",
        headers=admin_headers,
        json={"email": "invited@example.com", "role": "user", "expires_in_hours": 24},
    )
    assert invitation.status_code == 201, invitation.text
    token = invitation.json()["token"]
    stored = await db_session.scalar(select(Invitation).where(Invitation.id == invitation.json()["id"]))
    assert stored is not None
    assert token not in stored.token_hash
    assert "email" not in invitation.json()

    accepted = await auth_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": token, "password": "invited-password-123"},
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["user"]["email"] == "invited@example.com"

    reused = await auth_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": token, "password": "another-password-123"},
    )
    assert reused.status_code == 400
    assert reused.json()["detail"] == "invalid or expired invitation"


@pytest.mark.asyncio
async def test_admin_lifecycle_revokes_sessions_and_mcp_access(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_response = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "suspendable@example.com", "password": "correct horse battery staple"},
    )
    assert user_response.status_code == 201
    user_id = user_response.json()["user"]["id"]
    access_token = user_response.json()["access_token"]
    refresh_token = auth_client.cookies.get("tuxnews_refresh")
    assert refresh_token
    db_user = await db_session.get(User, user_id)
    assert db_user is not None
    _, agent_secret = await create_agent_token(
        db_session,
        user_id=user_id,
        name="suspendable-agent",
        scopes=["news:read"],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    admin_headers = await _admin_headers(auth_client, db_session)

    suspended = await auth_client.post(f"/api/v1/admin/users/{user_id}/suspend", headers=admin_headers)
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["is_active"] is False

    old_access = await auth_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"})
    assert old_access.status_code == 401
    old_refresh = await auth_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert old_refresh.status_code == 401

    @asynccontextmanager
    async def session_context():
        yield db_session

    monkeypatch.setattr("app.mcp.auth.SessionFactory", session_context)
    db_session.expire_all()
    assert await TuxnewsTokenVerifier().verify_token(access_token) is None
    assert await TuxnewsTokenVerifier().verify_token(agent_secret) is None

    reactivated = await auth_client.post(f"/api/v1/admin/users/{user_id}/reactivate", headers=admin_headers)
    assert reactivated.status_code == 200
    assert reactivated.json()["is_active"] is True
    db_session.expire_all()
    assert await TuxnewsTokenVerifier().verify_token(access_token) is None
    fresh_login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": "suspendable@example.com", "password": "correct horse battery staple"},
    )
    assert fresh_login.status_code == 200
    db_session.expire_all()
    assert await TuxnewsTokenVerifier().verify_token(fresh_login.json()["access_token"]) is not None


@pytest.mark.asyncio
async def test_deleting_user_keeps_audit_and_removes_owned_records(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def skip_vector_cleanup(_: int) -> None:
        return None

    monkeypatch.setattr("app.users.service._cleanup_user_vectors", skip_vector_cleanup)
    user = User(email="deletable@example.com", password_hash=hash_password("delete-password-123"))
    admin = User(
        email="deleting-admin@example.com",
        password_hash=hash_password("admin-password-123"),
        role="admin",
    )
    db_session.add_all([user, admin])
    await db_session.flush()
    db_session.add(
        Source(user_id=user.id, name="Owned source", url="https://example.com/feed", source_type="rss")
    )
    db_session.add(UserSession(
        user_id=user.id,
        refresh_token_hash="b" * 64,
        family_id="family",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    ))
    await db_session.commit()
    user_id = user.id
    login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": admin.email, "password": "admin-password-123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    deleted = await auth_client.delete(f"/api/v1/admin/users/{user_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    db_session.expire_all()
    assert await db_session.get(User, user_id) is None
    assert not await db_session.scalar(select(Source).where(Source.user_id == user_id))
    assert not await db_session.scalar(select(UserSession).where(UserSession.user_id == user_id))
    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "user.deleted", AuditEvent.resource_id == str(user_id))
    )
    assert audit is not None
    assert audit.user_id is None
    assert audit.details["actor_type"] == "admin"


@pytest.mark.asyncio
async def test_recovery_and_email_change_tokens_are_one_shot(
    db_session: AsyncSession,
) -> None:
    user = User(email="recover@example.com", password_hash=hash_password("old-password-123"))
    db_session.add(user)
    await db_session.flush()

    recovery_token = await issue_password_recovery(db_session, user_id=user.id, correlation_id="recovery")
    assert recovery_token
    stored_token = await db_session.scalar(select(UserActionToken).where(UserActionToken.user_id == user.id))
    assert stored_token is not None
    assert recovery_token not in stored_token.token_hash
    assert await complete_password_recovery(
        db_session,
        token=recovery_token,
        new_password="new-password-123",
        correlation_id="recovery-confirm",
    )
    assert not await complete_password_recovery(
        db_session,
        token=recovery_token,
        new_password="another-password-123",
        correlation_id="recovery-reuse",
    )

    email_token = await issue_email_change(
        db_session,
        user_id=user.id,
        new_email="changed@example.com",
        correlation_id="email-change",
    )
    assert email_token
    assert await complete_email_change(db_session, token=email_token, correlation_id="email-confirm")
    assert (await db_session.get(User, user.id)).email == "changed@example.com"
    assert not await complete_email_change(db_session, token=email_token, correlation_id="email-reuse")


@pytest.mark.asyncio
async def test_account_action_endpoints_do_not_enumerate_users(auth_client: AsyncClient) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "action-user@example.com", "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201
    known = await auth_client.post(
        "/api/v1/auth/password-recovery",
        json={"email": "action-user@example.com"},
    )
    unknown = await auth_client.post(
        "/api/v1/auth/password-recovery",
        json={"email": "missing-action-user@example.com"},
    )
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json() == {"status": "accepted"}

    wrong_password = await auth_client.post(
        "/api/v1/auth/email-change",
        headers={"Authorization": f"Bearer {registered.json()['access_token']}"},
        json={"new_email": "new-action-user@example.com", "current_password": "wrong"},
    )
    assert wrong_password.status_code == 401


@pytest.mark.asyncio
async def test_mcp_rejects_access_token_for_missing_user(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = create_access_token(999_999, scopes=["content:read"])

    @asynccontextmanager
    async def session_context():
        yield db_session

    monkeypatch.setattr("app.mcp.auth.SessionFactory", session_context)
    assert await TuxnewsTokenVerifier().verify_token(token) is None
