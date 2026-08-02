"""Track archive export attempts and safe failure state.

Revision ID: 0004_archive_export_state
Revises: 0003_article_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_archive_export_state"
down_revision: str | None = "0003_article_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("archive_exports")}
    if "attempts" not in columns:
        op.add_column("archive_exports", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
        op.alter_column("archive_exports", "attempts", server_default=None)
    if "error_message" not in columns:
        op.add_column("archive_exports", sa.Column("error_message", sa.String(length=500), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("archive_exports")}
    if "error_message" in columns:
        op.drop_column("archive_exports", "error_message")
    if "attempts" in columns:
        op.drop_column("archive_exports", "attempts")
