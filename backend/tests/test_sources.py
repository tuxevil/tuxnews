import ipaddress
from pathlib import Path

import pytest
from app.core.security import hash_password
from app.db.models import Source, User
from app.ingestion import http_client
from app.ingestion.sources import load_sources, sync_static_sources
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def test_loader_keeps_valid_entries_and_reports_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(
        """
sources:
  - name: "  Valid source  "
    url: https://example.com/feed
    tags: [news, news]
  - name: Broken source
    url: ftp://example.com/feed
  - name: Extra field
    url: https://example.org/feed
    unexpected: true
""",
        encoding="utf-8",
    )

    result = load_sources(path)

    assert [source.name for source in result.sources] == ["Valid source"]
    assert result.sources[0].tags == ["news"]
    assert len(result.errors) == 2


@pytest.mark.asyncio
async def test_static_sync_is_idempotent_and_preserves_dynamic_sources(
    db_session: AsyncSession,
    user_factory,
    tmp_path: Path,
) -> None:
    user = user_factory()
    db_session.add(user)
    await db_session.commit()
    path = tmp_path / "sources.yaml"
    path.write_text(
        """
sources:
  - name: Static one
    url: https://example.com/one
  - name: Static two
    url: https://example.com/two
""",
        encoding="utf-8",
    )

    first = await sync_static_sources(db_session, user.id, path)
    second = await sync_static_sources(db_session, user.id, path)
    assert not first.errors
    assert not second.errors

    dynamic = Source(
        user_id=user.id,
        name="Dynamic source",
        url="https://example.com/dynamic",
        origin="dynamic",
    )
    db_session.add(dynamic)
    await db_session.commit()

    path.write_text(
        """
sources:
  - name: Static one renamed
    url: https://example.com/one
""",
        encoding="utf-8",
    )
    await sync_static_sources(db_session, user.id, path)

    sources = list(await db_session.scalars(select(Source).where(Source.user_id == user.id).order_by(Source.url)))
    assert [(source.url, source.origin, source.is_active) for source in sources] == [
        ("https://example.com/dynamic", "dynamic", True),
        ("https://example.com/one", "static", True),
        ("https://example.com/two", "static", False),
    ]


async def public_resolver(_: str, __: int) -> list[ipaddress.IPv4Address]:
    return [ipaddress.ip_address("93.184.216.34")]


@pytest.mark.asyncio
async def test_source_crud_requires_scope_ownership_and_safe_urls(
    auth_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_client, "_resolve_host", public_resolver)
    registered = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "source-owner@example.com", "password": "correct horse battery staple"},
    )
    access_token = registered.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    private = await auth_client.post(
        "/api/v1/sources",
        headers=headers,
        json={"name": "Private", "url": "http://127.0.0.1/feed"},
    )
    assert private.status_code == 422

    created = await auth_client.post(
        "/api/v1/sources",
        headers=headers,
        json={"name": "Owner source", "url": "https://news.example.test/feed", "tags": ["news"]},
    )
    assert created.status_code == 201
    source_id = created.json()["id"]

    duplicate = await auth_client.post(
        "/api/v1/sources",
        headers=headers,
        json={"name": "Duplicate", "url": "https://news.example.test/feed"},
    )
    assert duplicate.status_code == 409

    updated = await auth_client.patch(
        f"/api/v1/sources/{source_id}",
        headers=headers,
        json={"name": "Renamed source", "is_active": False},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed source"

    other = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "other-owner@example.com", "password": "correct horse battery staple"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    hidden = await auth_client.get(f"/api/v1/sources/{source_id}", headers=other_headers)
    assert hidden.status_code == 404


@pytest.mark.asyncio
async def test_admin_can_list_sources_owned_by_other_users(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_client, "_resolve_host", public_resolver)
    owner = user_factory()
    admin = User(
        email="fixture-admin@example.com",
        password_hash=hash_password("fixture-admin-password"),
        role="admin",
    )
    db_session.add_all([owner, admin])
    await db_session.flush()
    db_session.add(source_factory(owner.id, url="https://example.com/admin-visible"))
    await db_session.commit()

    login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": "fixture-admin@example.com", "password": "fixture-admin-password"},
    )
    assert login.status_code == 200, login.text
    sources = await auth_client.get(
        "/api/v1/sources",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert sources.status_code == 200
    assert sources.json() == []
