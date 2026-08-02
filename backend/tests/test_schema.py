from app.db import models  # noqa: F401
from app.db.base import Base


def test_initial_schema_contains_core_aggregates() -> None:
    expected = {
        "users",
        "user_sessions",
        "agent_tokens",
        "user_invitations",
        "user_action_tokens",
        "sources",
        "articles",
        "clusters",
        "cluster_members",
        "feedbacks",
        "user_topics",
        "briefing_schedules",
        "briefings",
        "briefing_items",
        "archive_exports",
        "ingestion_runs",
        "usage_events",
        "audit_events",
    }
    assert expected.issubset(Base.metadata.tables)
    assert "user_id" in Base.metadata.tables["cluster_members"].c
    assert "user_id" in Base.metadata.tables["briefing_items"].c
