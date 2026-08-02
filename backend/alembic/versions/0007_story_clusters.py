"""Add temporal story cluster metadata and explainable memberships.

Revision ID: 0007_story_clusters
Revises: 0006_source_preferences
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_story_clusters"
down_revision: str | None = "0006_source_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _checks(table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
        if constraint.get("name")
    }


def upgrade() -> None:
    for name, column in (
        ("status", sa.Column("status", sa.String(length=16), nullable=False, server_default="active")),
        ("window_start", sa.Column("window_start", sa.DateTime(timezone=True), nullable=True)),
        ("window_end", sa.Column("window_end", sa.DateTime(timezone=True), nullable=True)),
        ("reconciled_at", sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True)),
    ):
        if name not in _columns("clusters"):
            op.add_column("clusters", column)
            if name == "status":
                op.alter_column("clusters", name, server_default=None)
    if "ck_clusters_status" not in _checks("clusters"):
        op.create_check_constraint(
            "ck_clusters_status",
            "clusters",
            "status IN ('active', 'stale', 'empty', 'ambiguous')",
        )
    if "cluster_members" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "cluster_members",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cluster_id", sa.Integer(), nullable=False),
            sa.Column("article_id", sa.Integer(), nullable=False),
            sa.Column("similarity_score", sa.Float(), nullable=False),
            sa.Column("membership_reason", sa.String(length=500), nullable=False),
            sa.Column("algorithm_version", sa.String(length=64), nullable=False),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["article_id"], ["articles.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("cluster_id", "article_id", "algorithm_version", name="uq_cluster_member_version"),
        )
        op.create_index("ix_cluster_members_cluster_id", "cluster_members", ["cluster_id"])
        op.create_index("ix_cluster_members_article_id", "cluster_members", ["article_id"])
        op.create_index("ix_cluster_members_is_current", "cluster_members", ["is_current"])


def downgrade() -> None:
    if "cluster_members" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_cluster_members_is_current", table_name="cluster_members")
        op.drop_index("ix_cluster_members_article_id", table_name="cluster_members")
        op.drop_index("ix_cluster_members_cluster_id", table_name="cluster_members")
        op.drop_table("cluster_members")
    if "ck_clusters_status" in _checks("clusters"):
        op.drop_constraint("ck_clusters_status", "clusters", type_="check")
    for name in ("reconciled_at", "window_end", "window_start", "status"):
        if name in _columns("clusters"):
            op.drop_column("clusters", name)
