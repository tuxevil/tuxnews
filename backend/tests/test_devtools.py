import pytest
from app.articles.lifecycle import ArticleStatus, publish_extracted_articles
from app.core.security import hash_password
from app.db.models import IngestionRun, User
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_trigger_ingestion_creates_run_and_enqueues(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    source_factory,
    monkeypatch,
) -> None:
    user = user_factory(email="ingest-user@example.com")
    user.password_hash = hash_password("valid-password")
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    db_session.add(source)
    await db_session.flush()
    await db_session.commit()

    calls: list[tuple[object, ...]] = []

    class FakePool:
        async def enqueue_job(self, *args: object) -> None:
            calls.append(args)

        async def aclose(self) -> None:
            return None

    async def fake_pool(_: object) -> FakePool:
        return FakePool()

    monkeypatch.setattr("app.ingestion.queue.create_pool", fake_pool)

    login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "valid-password"},
    )
    assert login.status_code == 200
    response = await auth_client.post(
        f"/api/v1/sources/{source.id}/ingest",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    run = await db_session.scalar(select(IngestionRun).where(IngestionRun.id == payload["run_id"]))
    assert run is not None
    assert run.source_id == source.id
    assert calls == [
        (
            "ingest_source",
            run.id,
            {
                "tenant_id": user.id,
                "actor_type": "user",
                "actor_id": str(user.id),
                "correlation_id": None,
            },
        )
    ]


@pytest.mark.asyncio
async def test_publish_extracted_articles_fast_forwards_to_published(
    db_session: AsyncSession,
    db_engine,
    user_factory,
    source_factory,
    article_factory,
    monkeypatch,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    monkeypatch.setattr(
        "app.articles.lifecycle.SessionFactory",
        async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False),
    )
    user = user_factory()
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    db_session.add(source)
    await db_session.flush()
    article = article_factory(user.id, source.id)
    article.status = ArticleStatus.EXTRACTED.value
    db_session.add(article)
    await db_session.commit()

    published = await publish_extracted_articles(user.id)

    assert published == 1
    await db_session.refresh(article)
    assert article.status == ArticleStatus.PUBLISHED.value
    assert article.published_stage_at is not None


@pytest.mark.asyncio
async def test_dev_publish_endpoint_gated_to_admin(
    auth_client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    monkeypatch,
) -> None:
    admin = User(email="dev-admin@example.com", password_hash=hash_password("dev-admin-password"), role="admin")
    db_session.add(admin)
    await db_session.commit()

    async def fake_publish(_: int) -> int:
        return 3

    monkeypatch.setattr("app.api.routes.devtools.publish_extracted_articles", fake_publish)

    login = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": admin.email, "password": "dev-admin-password"},
    )
    assert login.status_code == 200
    response = await auth_client.post(
        "/api/v1/admin/dev/publish-extracted",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )

    assert response.status_code == 200
    assert response.json() == {"published": 3}
