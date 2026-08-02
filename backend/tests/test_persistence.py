from datetime import UTC

import pytest
from app.db.models import Article, Source, User
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_factories_persist_related_entities_with_utc_timestamps(
    db_session: AsyncSession,
    user_factory,
    source_factory,
    article_factory,
    feedback_factory,
) -> None:
    user = user_factory()
    db_session.add(user)
    await db_session.flush()
    source = source_factory(user.id)
    db_session.add(source)
    await db_session.flush()
    article = article_factory(user.id, source.id)
    db_session.add(article)
    await db_session.flush()
    feedback = feedback_factory(user.id, article.id)
    db_session.add(feedback)
    await db_session.commit()

    persisted = await db_session.scalar(select(Article).where(Article.id == article.id))
    assert persisted is not None
    assert persisted.user_id == user.id
    assert persisted.source_id == source.id
    assert persisted.created_at.tzinfo is not None
    assert persisted.created_at.utcoffset() == UTC.utcoffset(persisted.created_at)


@pytest.mark.asyncio
async def test_duplicate_user_email_rolls_back_transaction(
    db_session: AsyncSession,
    user_factory,
) -> None:
    email = "duplicate@example.test"
    db_session.add_all([user_factory(email=email), user_factory(email=email)])

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    count = await db_session.scalar(select(func.count()).select_from(User).where(User.email == email))
    assert count == 0


@pytest.mark.asyncio
async def test_source_url_is_unique_per_owner_but_not_globally(
    db_session: AsyncSession,
    user_factory,
    source_factory,
) -> None:
    owner = user_factory()
    other_owner = user_factory()
    db_session.add_all([owner, other_owner])
    await db_session.flush()
    url = "https://example.test/shared-feed"
    db_session.add_all([source_factory(owner.id, url=url), source_factory(other_owner.id, url=url)])
    await db_session.commit()

    duplicate = source_factory(owner.id, url=url)
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    persisted = await db_session.scalar(select(func.count()).select_from(Source).where(Source.url == url))
    assert persisted == 2
