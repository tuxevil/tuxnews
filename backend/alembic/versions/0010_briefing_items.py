"""Add briefing generation metadata and provenance items."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_briefing_items"
down_revision: str | None = "0009_discovery_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    columns = _columns("briefings")
    if "generation_version" not in columns:
        op.add_column(
            "briefings",
            sa.Column("generation_version", sa.String(length=64), nullable=False, server_default="briefing-v1"),
        )
        op.alter_column("briefings", "generation_version", server_default=None)
    if "revision" not in columns:
        op.add_column("briefings", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
        op.alter_column("briefings", "revision", server_default=None)
    if "checksum" not in columns:
        op.add_column("briefings", sa.Column("checksum", sa.String(length=64), nullable=True))
    if "error_message" not in columns:
        op.add_column("briefings", sa.Column("error_message", sa.String(length=1000), nullable=True))

    if "briefing_items" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "briefing_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("briefing_id", sa.Integer(), nullable=False),
            sa.Column("article_id", sa.Integer(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("display_rank", sa.Float(), nullable=False),
            sa.Column("provenance_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["briefing_id"], ["briefings.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("briefing_id", "article_id", name="uq_briefing_items_article"),
            sa.CheckConstraint("article_id IS NOT NULL", name="ck_briefing_items_article_required"),
        )
        op.create_index("ix_briefing_items_briefing_id", "briefing_items", ["briefing_id"])
        op.create_index("ix_briefing_items_article_id", "briefing_items", ["article_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "briefing_items" in inspector.get_table_names():
        op.drop_index("ix_briefing_items_article_id", table_name="briefing_items")
        op.drop_index("ix_briefing_items_briefing_id", table_name="briefing_items")
        op.drop_table("briefing_items")
    columns = _columns("briefings")
    for name in ("error_message", "checksum", "revision", "generation_version"):
        if name in columns:
            op.drop_column("briefings", name)
