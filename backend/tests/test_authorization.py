import pytest
from app.api.deps import IdentityContext, get_owned_or_404, require_role, require_scope
from app.core.permissions import Scope, has_scope, scopes_for_role
from app.db.models import Source, User
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession


def _identity(user: User, *scopes: str) -> IdentityContext:
    return IdentityContext(user=user, token={}, scopes=frozenset(scopes))


def test_role_scopes_are_explicit_and_admin_is_wildcard() -> None:
    user_scopes = scopes_for_role("user")
    admin_scopes = scopes_for_role("admin")

    assert Scope.CONTENT_READ in user_scopes
    assert Scope.USERS_MANAGE not in user_scopes
    assert has_scope(admin_scopes, Scope.USERS_MANAGE)
    assert not has_scope(frozenset(), Scope.CONTENT_READ)


@pytest.mark.asyncio
async def test_require_scope_and_role_reject_missing_permissions() -> None:
    user = User(id=1, email="reader@example.com", password_hash="hash", role="user")
    identity = _identity(user)

    with pytest.raises(HTTPException) as scope_error:
        await require_scope(Scope.CONTENT_READ)(identity)
    assert scope_error.value.status_code == 403

    with pytest.raises(HTTPException) as role_error:
        await require_role("admin")(identity)
    assert role_error.value.status_code == 403


@pytest.mark.asyncio
async def test_owned_lookup_hides_cross_user_resources_for_admin_too(
    db_session: AsyncSession,
    user_factory,
    source_factory,
    identity_factory,
) -> None:
    owner = user_factory()
    other = user_factory()
    admin = user_factory(role="admin")
    db_session.add_all([owner, other, admin])
    await db_session.flush()
    source = source_factory(owner.id, url="https://example.test/owned")
    db_session.add(source)
    await db_session.commit()

    with pytest.raises(HTTPException) as hidden:
        await get_owned_or_404(db_session, Source, source.id, identity_factory(other, Scope.SOURCES_READ))
    assert hidden.value.status_code == 404

    with pytest.raises(HTTPException) as admin_hidden:
        await get_owned_or_404(db_session, Source, source.id, identity_factory(admin, "*"))
    assert admin_hidden.value.status_code == 404
