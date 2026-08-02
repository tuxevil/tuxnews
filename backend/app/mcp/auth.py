from __future__ import annotations

from datetime import UTC, datetime

import jwt
from fastmcp.server.auth import AccessToken, TokenVerifier
from sqlalchemy import select

from app.core.permissions import AgentScope, scopes_for_role
from app.core.security import decode_token, hash_agent_token, token_is_revoked
from app.db.models import AgentToken, User
from app.db.session import SessionFactory

MCP_ALLOWED_SCOPES = frozenset(scope.value for scope in AgentScope)


class TuxnewsTokenVerifier(TokenVerifier):
    """Validate the existing short-lived access JWTs for the MCP transport.

    Agent tokens are checked against their hashed database record so revocation
    and expiration apply to every new MCP request.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = decode_token(token, "access")
            user_id = int(claims["sub"])
            raw_scopes = claims.get("scopes", [])
            expires_at = claims.get("exp")
            if (
                not isinstance(raw_scopes, list)
                or not all(isinstance(scope, str) for scope in raw_scopes)
                or not isinstance(expires_at, (int, float))
            ):
                return None
            scopes = frozenset(raw_scopes)
            if not scopes:
                return None
        except (ValueError, KeyError, TypeError, jwt.InvalidTokenError):
            pass
        else:
            async with SessionFactory() as session:
                user = await session.scalar(
                    select(User).where(User.id == user_id, User.is_active.is_(True), User.deleted_at.is_(None))
                )
            if user is None:
                return None
            if token_is_revoked(claims, user.tokens_revoked_at):
                return None
            effective_scopes = scopes.intersection(scopes_for_role(user.role))
            if not effective_scopes:
                return None
            return AccessToken(
                token=token,
                client_id=f"user:{user_id}",
                scopes=sorted(effective_scopes),
                expires_at=int(expires_at),
                claims=claims if isinstance(claims, dict) else {},
            )

        async with SessionFactory() as session:
            agent_token = await session.scalar(
                select(AgentToken)
                .join(User, User.id == AgentToken.user_id)
                .where(
                    AgentToken.token_hash == hash_agent_token(token),
                    AgentToken.revoked_at.is_(None),
                    User.is_active.is_(True),
                )
            )
            if agent_token is None:
                return None
            expires_at = agent_token.expires_at
            if expires_at is not None:
                expires = expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                if expires <= datetime.now(UTC):
                    return None
                expires_at = expires
            if not isinstance(agent_token.scopes, list) or not all(
                isinstance(scope, str) and scope in MCP_ALLOWED_SCOPES for scope in agent_token.scopes
            ):
                return None
            agent_scopes = sorted(set(agent_token.scopes))
            if not agent_scopes:
                return None

        return AccessToken(
            token=token,
            client_id=f"agent:{agent_token.id}",
            scopes=agent_scopes,
            expires_at=int(expires_at.timestamp()) if expires_at else None,
            claims={
                "sub": str(agent_token.user_id),
                "type": "agent",
                "agent_token_id": agent_token.id,
                "scopes": agent_scopes,
            },
        )
