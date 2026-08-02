"""Track whether a source is repo-managed or user-managed.

Revision ID: 0002_source_origin
Revises: 0001_schema
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_source_origin"
down_revision: str | None = "0001_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sources")}
    if "origin" not in columns:
        op.add_column(
            "sources",
            sa.Column("origin", sa.String(length=16), nullable=False, server_default="dynamic"),
        )
        op.alter_column("sources", "origin", server_default=None)


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sources")}
    if "origin" in columns:
        op.drop_column("sources", "origin")
