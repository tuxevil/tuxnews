"""Add user lifecycle, invitation, and account action state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_user_lifecycle"
down_revision: str | None = "0010_briefing_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    user_columns = _columns("users")
    if "suspended_at" not in user_columns:
        op.add_column("users", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
    if "deleted_at" not in user_columns:
        op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    if "tokens_revoked_at" not in user_columns:
        op.add_column("users", sa.Column("tokens_revoked_at", sa.DateTime(timezone=True), nullable=True))

    tables = _tables()
    if "user_invitations" not in tables:
        op.create_table(
            "user_invitations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("email", sa.String(length=320), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=False, server_default="user"),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("invited_by_user_id", sa.Integer(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["invited_by_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("token_hash", name="uq_user_invitations_token_hash"),
            sa.CheckConstraint("role IN ('user', 'admin')", name="ck_user_invitations_role"),
        )
        op.create_index("ix_user_invitations_email", "user_invitations", ["email"])
        op.create_index("ix_user_invitations_invited_by_user_id", "user_invitations", ["invited_by_user_id"])

    if "user_action_tokens" not in tables:
        op.create_table(
            "user_action_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("purpose", sa.String(length=32), nullable=False),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("target_email", sa.String(length=320), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("token_hash", name="uq_user_action_tokens_token_hash"),
            sa.CheckConstraint(
                "purpose IN ('password_recovery', 'email_change')",
                name="ck_user_action_tokens_purpose",
            ),
        )
        op.create_index("ix_user_action_tokens_user_id", "user_action_tokens", ["user_id"])
        op.create_index(
            "ix_user_action_tokens_user_purpose",
            "user_action_tokens",
            ["user_id", "purpose"],
        )


def downgrade() -> None:
    tables = _tables()
    if "user_action_tokens" in tables:
        op.drop_index("ix_user_action_tokens_user_purpose", table_name="user_action_tokens")
        op.drop_index("ix_user_action_tokens_user_id", table_name="user_action_tokens")
        op.drop_table("user_action_tokens")
    if "user_invitations" in tables:
        op.drop_index("ix_user_invitations_invited_by_user_id", table_name="user_invitations")
        op.drop_index("ix_user_invitations_email", table_name="user_invitations")
        op.drop_table("user_invitations")
    columns = _columns("users")
    if "deleted_at" in columns:
        op.drop_column("users", "deleted_at")
    if "tokens_revoked_at" in columns:
        op.drop_column("users", "tokens_revoked_at")
    if "suspended_at" in columns:
        op.drop_column("users", "suspended_at")
