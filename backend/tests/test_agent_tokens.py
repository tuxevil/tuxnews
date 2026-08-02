from contextlib import asynccontextmanager

import pytest
from app.agent_tokens.service import (
    create_agent_token,
    list_agent_tokens,
    revoke_agent_token,
    rotate_agent_token,
)
from app.core.permissions import AgentScope
from app.core.security import hash_agent_token
from app.db.models import AuditEvent
from app.mcp.auth import TuxnewsTokenVerifier
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_agent_token_lifecycle_is_hashed_revocable_and_audited(
    db_session: AsyncSession,
    user_factory,
    monkeypatch,
) -> None:
    user = user_factory()
    db_session.add(user)
    await db_session.flush()

    token, secret = await create_agent_token(
        db_session,
        user_id=user.id,
        name="Research agent",
        scopes=[AgentScope.NEWS_READ.value],
        correlation_id="request-123",
    )
    assert secret.startswith("tn_agent_")
    assert token.token_hash == hash_agent_token(secret)
    assert secret not in token.token_hash

    @asynccontextmanager
    async def session_context():
        yield db_session

    monkeypatch.setattr("app.mcp.auth.SessionFactory", session_context)
    verified = await TuxnewsTokenVerifier().verify_token(secret)
    assert verified is not None
    assert verified.client_id == f"agent:{token.id}"
    assert verified.scopes == [AgentScope.NEWS_READ.value]

    replacement, replacement_secret = await rotate_agent_token(
        db_session,
        user_id=user.id,
        token_id=token.id,
        correlation_id="request-124",
    ) or (None, None)
    assert replacement is not None
    assert replacement_secret is not None
    assert await TuxnewsTokenVerifier().verify_token(secret) is None
    assert await TuxnewsTokenVerifier().verify_token(replacement_secret) is not None

    revoked = await revoke_agent_token(
        db_session,
        user_id=user.id,
        token_id=replacement.id,
        correlation_id="request-125",
    )
    assert revoked is not None
    assert await TuxnewsTokenVerifier().verify_token(replacement_secret) is None

    audits = list(
        await db_session.scalars(
            select(AuditEvent).where(AuditEvent.user_id == user.id).order_by(AuditEvent.id)
        )
    )
    assert [audit.action for audit in audits] == [
        "agent_token.created",
        "agent_token.rotated",
        "agent_token.revoked",
    ]
    assert audits[1].details["tenant_id"] == user.id
    assert audits[1].details["actor_type"] == "user"
    assert secret not in str(audits)

    tokens = await list_agent_tokens(db_session, user_id=user.id)
    assert len(tokens) == 2


@pytest.mark.asyncio
async def test_agent_token_endpoints_return_secret_only_on_create_and_rotate(auth_client) -> None:
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "agent-token@example.com", "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    created = await auth_client.post(
        "/api/v1/agent-tokens",
        headers=headers,
        json={"name": "Local agent", "scopes": ["news:read"]},
    )
    assert created.status_code == 201
    secret = created.json()["token"]
    assert secret.startswith("tn_agent_")

    listed = await auth_client.get("/api/v1/agent-tokens", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["name"] == "Local agent"
    assert "token" not in listed.json()[0]

    rotated = await auth_client.post(
        f"/api/v1/agent-tokens/{created.json()['id']}/rotate",
        headers=headers,
    )
    assert rotated.status_code == 200
    assert rotated.json()["token"] != secret

    revoked = await auth_client.delete(
        f"/api/v1/agent-tokens/{rotated.json()['id']}",
        headers=headers,
    )
    assert revoked.status_code == 200
    assert "token" not in revoked.json()
