"""Persist the per-user display ranking mix."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_display_ranking"
down_revision: str | None = "0007_story_clusters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}


def upgrade() -> None:
    columns = _columns()
    if "serendipity_score" not in columns:
        op.add_column("users", sa.Column("serendipity_score", sa.Float(), nullable=False, server_default="0.25"))
        op.alter_column("users", "serendipity_score", server_default=None)
    if "ranking_preference_version" not in columns:
        op.add_column(
            "users",
            sa.Column("ranking_preference_version", sa.Integer(), nullable=False, server_default="0"),
        )
        op.alter_column("users", "ranking_preference_version", server_default=None)


def downgrade() -> None:
    columns = _columns()
    if "ranking_preference_version" in columns:
        op.drop_column("users", "ranking_preference_version")
    if "serendipity_score" in columns:
        op.drop_column("users", "serendipity_score")
