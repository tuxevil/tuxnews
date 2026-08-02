"""Track tenant-scoped LLM usage and protect event history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_llm_usage_events"
down_revision: str | None = "0013_audit_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("usage_events")}


def _indexes() -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("usage_events")}


def _add_column(name: str, column: sa.Column[object]) -> None:
    if name not in _columns():
        op.add_column("usage_events", column)


def upgrade() -> None:
    _add_column("tenant_id", sa.Column("tenant_id", sa.Integer(), nullable=True))
    _add_column("actor_type", sa.Column("actor_type", sa.String(length=32), nullable=True))
    _add_column("actor_id", sa.Column("actor_id", sa.String(length=120), nullable=True))
    _add_column("operation", sa.Column("operation", sa.String(length=120), nullable=True))
    _add_column("cost_is_estimated", sa.Column("cost_is_estimated", sa.Boolean(), nullable=True))
    _add_column("cost_currency", sa.Column("cost_currency", sa.String(length=8), nullable=True))
    _add_column("latency_ms", sa.Column("latency_ms", sa.Integer(), nullable=True))
    _add_column("outcome", sa.Column("outcome", sa.String(length=24), nullable=True))
    _add_column("used_fallback", sa.Column("used_fallback", sa.Boolean(), nullable=True))
    _add_column("attempt_count", sa.Column("attempt_count", sa.Integer(), nullable=True))
    _add_column("error_code", sa.Column("error_code", sa.String(length=120), nullable=True))
    _add_column("provider_request_id", sa.Column("provider_request_id", sa.String(length=160), nullable=True))
    _add_column("correlation_id", sa.Column("correlation_id", sa.String(length=64), nullable=True))

    op.execute(
        sa.text(
            "UPDATE usage_events SET "
            "tenant_id = COALESCE(tenant_id, user_id), "
            "actor_type = COALESCE(actor_type, 'user'), "
            "actor_id = COALESCE(actor_id, CAST(user_id AS VARCHAR)), "
            "operation = COALESCE(operation, 'legacy'), "
            "cost_is_estimated = COALESCE(cost_is_estimated, TRUE), "
            "cost_currency = COALESCE(cost_currency, 'USD'), "
            "latency_ms = COALESCE(latency_ms, 0), "
            "outcome = COALESCE(outcome, 'success'), "
            "used_fallback = COALESCE(used_fallback, FALSE), "
            "attempt_count = COALESCE(attempt_count, 1)"
        )
    )
    for name in (
        "tenant_id",
        "actor_type",
        "actor_id",
        "operation",
        "cost_is_estimated",
        "cost_currency",
        "latency_ms",
        "outcome",
        "used_fallback",
        "attempt_count",
    ):
        op.alter_column("usage_events", name, nullable=False)

    indexes = _indexes()
    if "ix_usage_events_tenant_created_at" not in indexes:
        op.create_index(
            "ix_usage_events_tenant_created_at",
            "usage_events",
            ["tenant_id", "created_at"],
        )
    if "ix_usage_events_provider_model_created_at" not in indexes:
        op.create_index(
            "ix_usage_events_provider_model_created_at",
            "usage_events",
            ["provider", "model", "created_at"],
        )
    if "ix_usage_events_correlation_id" not in indexes:
        op.create_index("ix_usage_events_correlation_id", "usage_events", ["correlation_id"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS tuxnews_usage_events_append_only_trigger ON usage_events"))
        op.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION tuxnews_usage_events_append_only() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "IF TG_OP = 'DELETE' AND current_setting('tuxnews.usage_maintenance', true) = 'on' "
                "THEN RETURN OLD; END IF; "
                "RAISE EXCEPTION 'usage_events is append-only'; END; $$"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER tuxnews_usage_events_append_only_trigger "
                "BEFORE UPDATE OR DELETE ON usage_events FOR EACH ROW "
                "EXECUTE FUNCTION tuxnews_usage_events_append_only()"
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS tuxnews_usage_events_append_only_trigger ON usage_events"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS tuxnews_usage_events_append_only()"))
    indexes = _indexes()
    for name in ("ix_usage_events_provider_model_created_at", "ix_usage_events_tenant_created_at"):
        if name in indexes:
            op.drop_index(name, table_name="usage_events")
    columns = _columns()
    for name in (
        "provider_request_id",
        "error_code",
        "attempt_count",
        "used_fallback",
        "outcome",
        "latency_ms",
        "cost_currency",
        "cost_is_estimated",
        "operation",
        "actor_id",
        "actor_type",
        "tenant_id",
    ):
        if name in columns:
            op.drop_column("usage_events", name)
