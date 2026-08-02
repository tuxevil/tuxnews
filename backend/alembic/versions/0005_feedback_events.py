"""Make feedback append-only and target-specific.

Revision ID: 0005_feedback_events
Revises: 0004_archive_export_state
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_feedback_events"
down_revision: str | None = "0004_archive_export_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> dict[str, dict[str, object]]:
    return {column["name"]: column for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _constraint_names(table_name: str, kind: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    constraints = {
        "unique": inspector.get_unique_constraints(table_name),
        "check": inspector.get_check_constraints(table_name),
        "foreignkey": inspector.get_foreign_keys(table_name),
    }[kind]
    return {constraint["name"] for constraint in constraints if constraint.get("name")}


def _index_names(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name) if index.get("name")}


def upgrade() -> None:
    if "uq_feedback_action" in _constraint_names("feedbacks", "unique"):
        op.drop_constraint("uq_feedback_action", "feedbacks", type_="unique")
    article_id = _columns("feedbacks").get("article_id")
    if article_id is not None and not article_id["nullable"]:
        op.alter_column("feedbacks", "article_id", nullable=True)

    for table_name, column_name in (
        ("sources", "reputation_version"),
        ("articles", "feedback_version"),
        ("user_topics", "preference_version"),
    ):
        if column_name not in _columns(table_name):
            op.add_column(table_name, sa.Column(column_name, sa.Integer(), nullable=False, server_default="0"))
            op.alter_column(table_name, column_name, server_default=None)
    for column_name, column in (
        ("source_id", sa.Column("source_id", sa.Integer(), nullable=True)),
        ("topic_name", sa.Column("topic_name", sa.String(length=200), nullable=True)),
        ("supersedes_id", sa.Column("supersedes_id", sa.Integer(), nullable=True)),
        ("is_current", sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true())),
    ):
        if column_name not in _columns("feedbacks"):
            op.add_column("feedbacks", column)
            if column_name == "is_current":
                op.alter_column("feedbacks", column_name, server_default=None)

    if "fk_feedbacks_source_id_sources" not in _constraint_names("feedbacks", "foreignkey"):
        op.create_foreign_key(
            "fk_feedbacks_source_id_sources",
            "feedbacks",
            "sources",
            ["source_id"],
            ["id"],
            ondelete="CASCADE",
        )
    if "fk_feedbacks_supersedes_id_feedbacks" not in _constraint_names("feedbacks", "foreignkey"):
        op.create_foreign_key(
            "fk_feedbacks_supersedes_id_feedbacks",
            "feedbacks",
            "feedbacks",
            ["supersedes_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "ck_feedback_target" not in _constraint_names("feedbacks", "check"):
        op.create_check_constraint(
            "ck_feedback_target",
            "feedbacks",
            "(action_type IN ('article', 'quality') AND article_id IS NOT NULL AND "
            "source_id IS NULL AND topic_name IS NULL) OR "
            "(action_type = 'source' AND article_id IS NULL AND source_id IS NOT NULL AND "
            "topic_name IS NULL) OR "
            "(action_type = 'topic' AND article_id IS NULL AND source_id IS NULL AND "
            "topic_name IS NOT NULL)",
        )
    indexes = _index_names("feedbacks")
    for index_name, columns, where in (
        ("uq_feedback_current_article", ["user_id", "article_id", "action_type"], "is_current"),
        ("uq_feedback_current_source", ["user_id", "source_id", "action_type"], "is_current"),
        ("uq_feedback_current_topic", ["user_id", "topic_name", "action_type"], "is_current"),
    ):
        if index_name not in indexes:
            op.create_index(
                index_name,
                "feedbacks",
                columns,
                unique=True,
                postgresql_where=sa.text(where),
                sqlite_where=sa.text(f"{where} = 1"),
            )


def downgrade() -> None:
    for index_name in (
        "uq_feedback_current_topic",
        "uq_feedback_current_source",
        "uq_feedback_current_article",
    ):
        if index_name in _index_names("feedbacks"):
            op.drop_index(index_name, table_name="feedbacks")
    if "ck_feedback_target" in _constraint_names("feedbacks", "check"):
        op.drop_constraint("ck_feedback_target", "feedbacks", type_="check")
    for constraint_name in (
        "fk_feedbacks_supersedes_id_feedbacks",
        "fk_feedbacks_source_id_sources",
    ):
        if constraint_name in _constraint_names("feedbacks", "foreignkey"):
            op.drop_constraint(constraint_name, "feedbacks", type_="foreignkey")
    for column_name in ("is_current", "supersedes_id", "topic_name", "source_id"):
        if column_name in _columns("feedbacks"):
            op.drop_column("feedbacks", column_name)
    article_id = _columns("feedbacks").get("article_id")
    if article_id is not None and article_id["nullable"]:
        op.alter_column("feedbacks", "article_id", nullable=False)
    for table_name, column_name in (
        ("user_topics", "preference_version"),
        ("articles", "feedback_version"),
        ("sources", "reputation_version"),
    ):
        if column_name in _columns(table_name):
            op.drop_column(table_name, column_name)
    if "uq_feedback_action" not in _constraint_names("feedbacks", "unique"):
        op.create_unique_constraint("uq_feedback_action", "feedbacks", ["user_id", "article_id", "action_type"])
