import asyncio
import os
from collections.abc import AsyncIterator, Callable
from itertools import count
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from app.api.deps import IdentityContext, get_session
from app.core.config import get_settings
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models import Article, Feedback, Source, User
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    database_url = os.getenv("TUXNEWS_TEST_DATABASE_URL", "sqlite+aiosqlite://")
    if not database_url.startswith("sqlite+aiosqlite://"):
        database_name = database_url.split("?", 1)[0].rsplit("/", 1)[-1]
        if (
            os.getenv("TUXNEWS_ALLOW_TEST_DATABASE_RESET") != "true"
            or not database_name.endswith("_test")
        ):
            raise RuntimeError(
                "Refusing to reset a non-test database; use a *_test database and "
                "set TUXNEWS_ALLOW_TEST_DATABASE_RESET=true"
            )
    engine_options: dict[str, object] = {}
    if database_url.startswith("sqlite+aiosqlite://"):
        engine_options["connect_args"] = {"check_same_thread": False}
        engine_options["poolclass"] = StaticPool
    engine = create_async_engine(database_url, **engine_options)
    if database_url.startswith("sqlite+aiosqlite://"):
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    else:
        os.environ["TUXNEWS_DATABASE_URL"] = database_url
        get_settings.cache_clear()
        config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        await asyncio.to_thread(command.upgrade, config, "head")
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            if not database_url.startswith("sqlite+aiosqlite://"):
                await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def auth_client(db_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def user_factory() -> Callable[..., User]:
    sequence = count(1)

    def build(*, role: str = "user", email: str | None = None) -> User:
        number = next(sequence)
        return User(
            email=email or f"fixture-user-{number}@example.test",
            password_hash="fixture-password-hash",
            role=role,
        )

    return build


@pytest.fixture
def source_factory() -> Callable[..., Source]:
    sequence = count(1)

    def build(user_id: int, *, url: str | None = None) -> Source:
        number = next(sequence)
        return Source(
            user_id=user_id,
            name=f"Fixture source {number}",
            url=url or f"https://example.test/feed/{number}",
        )

    return build


@pytest.fixture
def article_factory() -> Callable[..., Article]:
    sequence = count(1)

    def build(user_id: int, source_id: int) -> Article:
        number = next(sequence)
        url = f"https://example.test/article/{number}"
        return Article(
            user_id=user_id,
            source_id=source_id,
            title=f"Fixture article {number}",
            original_title=f"Fixture article {number}",
            url=url,
            canonical_url_hash=f"{number:064d}",
        )

    return build


@pytest.fixture
def feedback_factory() -> Callable[..., Feedback]:
    def build(user_id: int, article_id: int, *, rating: str = "like") -> Feedback:
        return Feedback(user_id=user_id, article_id=article_id, rating=rating)

    return build


@pytest.fixture
def identity_factory() -> Callable[..., IdentityContext]:
    def build(user: User, *scopes: str) -> IdentityContext:
        return IdentityContext(user=user, token={}, scopes=frozenset(scopes))

    return build
