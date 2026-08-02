from datetime import UTC, datetime, timedelta

import pytest
from app import worker
from app.db.models import Briefing, BriefingSchedule, Source, User
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


class FakeRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def enqueue_job(self, function: str, *args: object, **kwargs: object) -> None:
        self.jobs.append((function, args, kwargs))


@pytest.mark.asyncio
async def test_scheduler_dispatches_due_source_and_discovery_work(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(email="scheduler@example.com", password_hash="fixture-password-hash")
    db_session.add(user)
    await db_session.flush()
    source = Source(
        user_id=user.id,
        name="Due source",
        url="https://example.com/feed.xml",
        last_fetched_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(
        Source(
            user_id=user.id,
            name="Discovery source",
            url="https://html.duckduckgo.com/html/",
            origin="discovery",
            last_fetched_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    db_session.add(source)
    await db_session.commit()

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionFactory", session_factory)
    source_calls: list[tuple[int, str]] = []

    async def fake_enqueue_source_ingestion(session, *, source, actor, pool) -> int:
        source_calls.append((source.id, actor.actor_type))
        return 91

    monkeypatch.setattr(worker, "enqueue_source_ingestion", fake_enqueue_source_ingestion)
    redis = FakeRedis()

    result = await worker.schedule_due_work({"redis": redis})

    assert result == {"sources": 1, "discovery": 1, "briefings": 0}
    assert source_calls == [(source.id, "scheduler")]
    assert len(redis.jobs) == 1
    function, args, options = redis.jobs[0]
    assert function == "discover_user"
    assert args[0] == user.id
    assert options["_job_id"].startswith(f"discovery:{user.id}:")


@pytest.mark.asyncio
async def test_scheduler_dispatches_briefing_at_user_local_time_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    user = User(email="briefing-scheduler@example.com", password_hash="fixture-password-hash")
    db_session.add(user)
    await db_session.flush()
    schedule = BriefingSchedule(user_id=user.id, local_time="08:00", timezone="UTC", is_active=True)
    db_session.add(schedule)
    await db_session.commit()
    redis = FakeRedis()
    now = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)

    first = await worker._schedule_briefings(db_session, redis, now=now)

    assert first == 1
    assert redis.jobs[0][0] == "generate_briefing"
    assert redis.jobs[0][1][:4] == (user.id, "2026-08-02", "08:00", "UTC")
    assert redis.jobs[0][2]["_job_id"] == f"briefing:{user.id}:2026-08-02:08:00"

    db_session.add(
        Briefing(
            user_id=user.id,
            briefing_date="2026-08-02",
            local_time="08:00",
            timezone="UTC",
            title="Morning brief",
            content_markdown="No stories.",
        )
    )
    await db_session.commit()

    second = await worker._schedule_briefings(db_session, redis, now=now)

    assert second == 0
    assert len(redis.jobs) == 1
