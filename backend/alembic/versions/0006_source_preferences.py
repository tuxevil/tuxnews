"""Add user-controlled source mute state and preference versions.

Revision ID: 0006_source_preferences
Revises: 0005_feedback_events
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_source_preferences"
down_revision: str | None = "0005_feedback_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sources")}


def upgrade() -> None:
    if "is_muted" not in _columns():
        op.add_column("sources", sa.Column("is_muted", sa.Boolean(), nullable=False, server_default=sa.false()))
        op.alter_column("sources", "is_muted", server_default=None)
    if "preference_version" not in _columns():
        op.add_column("sources", sa.Column("preference_version", sa.Integer(), nullable=False, server_default="0"))
        op.alter_column("sources", "preference_version", server_default=None)


def downgrade() -> None:
    columns = _columns()
    if "preference_version" in columns:
        op.drop_column("sources", "preference_version")
    if "is_muted" in columns:
        op.drop_column("sources", "is_muted")
