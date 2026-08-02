"""Add typed audit identity and protect audit rows from mutation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_audit_integrity"
down_revision: str | None = "0012_tenant_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("audit_events")}


def upgrade() -> None:
    columns = _columns()
    if op.get_bind().dialect.name == "postgresql":
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys("audit_events"):
            if foreign_key.get("constrained_columns") == ["user_id"] and foreign_key.get("name"):
                op.drop_constraint(foreign_key["name"], "audit_events", type_="foreignkey")
    if "tenant_id" not in columns:
        op.add_column("audit_events", sa.Column("tenant_id", sa.Integer(), nullable=True))
    if "actor_type" not in columns:
        op.add_column(
            "audit_events",
            sa.Column("actor_type", sa.String(length=32), nullable=True, server_default="user"),
        )
        op.execute(sa.text("UPDATE audit_events SET actor_type = 'user' WHERE actor_type IS NULL"))
        if op.get_bind().dialect.name == "postgresql":
            op.alter_column("audit_events", "actor_type", nullable=False, server_default=None)
    if "actor_id" not in columns:
        op.add_column("audit_events", sa.Column("actor_id", sa.String(length=120), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text(
                "UPDATE audit_events SET tenant_id = NULLIF(details->>'tenant_id', '')::integer "
                "WHERE tenant_id IS NULL AND (details::jsonb) ? 'tenant_id'"
            )
        )
        op.execute(
            sa.text(
                "UPDATE audit_events SET actor_id = NULLIF(details->>'actor_id', '') "
                "WHERE actor_id IS NULL AND (details::jsonb) ? 'actor_id'"
            )
        )
        op.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION tuxnews_audit_events_append_only() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit_events is append-only'; END; $$"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER tuxnews_audit_events_append_only_trigger "
                "BEFORE UPDATE OR DELETE ON audit_events FOR EACH ROW "
                "EXECUTE FUNCTION tuxnews_audit_events_append_only()"
            )
        )
    else:
        op.execute(sa.text("UPDATE audit_events SET tenant_id = user_id WHERE tenant_id IS NULL"))
        op.execute(sa.text("UPDATE audit_events SET actor_id = CAST(user_id AS VARCHAR) WHERE actor_id IS NULL"))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS tuxnews_audit_events_append_only_trigger ON audit_events"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS tuxnews_audit_events_append_only()"))
    columns = _columns()
    if "actor_id" in columns:
        op.drop_column("audit_events", "actor_id")
    if "actor_type" in columns:
        op.drop_column("audit_events", "actor_type")
    if "tenant_id" in columns:
        op.drop_column("audit_events", "tenant_id")
