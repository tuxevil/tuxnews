"""Persist idempotent per-user discovery slots."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_discovery_runs"
down_revision: str | None = "0008_display_ranking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "discovery_runs" in inspector.get_table_names():
        return
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("slot_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("provider_version", sa.String(length=80), nullable=False),
        sa.Column("serendipity_score", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("query_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.String(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "slot_key", name="uq_discovery_runs_user_slot"),
    )
    op.create_index("ix_discovery_runs_user_id", "discovery_runs", ["user_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "discovery_runs" not in inspector.get_table_names():
        return
    op.drop_index("ix_discovery_runs_user_id", table_name="discovery_runs")
    op.drop_table("discovery_runs")
