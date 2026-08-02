"""Add explicit article lifecycle state metadata.

Revision ID: 0003_article_lifecycle
Revises: 0002_source_origin
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_article_lifecycle"
down_revision: str | None = "0002_source_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("articles")}
    definitions = {
        "status_error": sa.Column("status_error", sa.String(length=1000), nullable=True),
        "discovered_at": sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True),
        "fetch_started_at": sa.Column("fetch_started_at", sa.DateTime(timezone=True), nullable=True),
        "extracted_at": sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        "curated_at": sa.Column("curated_at", sa.DateTime(timezone=True), nullable=True),
        "indexed_at": sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        "published_stage_at": sa.Column("published_stage_at", sa.DateTime(timezone=True), nullable=True),
    }
    for name, column in definitions.items():
        if name not in columns:
            op.add_column("articles", column)
    if "discovered_at" not in columns:
        op.execute(sa.text("UPDATE articles SET discovered_at = created_at WHERE discovered_at IS NULL"))
        op.alter_column("articles", "discovered_at", nullable=False)
    if "ck_articles_status" not in {
        constraint["name"] for constraint in sa.inspect(op.get_bind()).get_check_constraints("articles")
    }:
        op.create_check_constraint(
            "ck_articles_status",
            "articles",
            "status IN ('discovered', 'fetching', 'extracted', 'curated', 'indexed', 'published', 'failed')",
        )


def downgrade() -> None:
    if "ck_articles_status" in {
        constraint["name"] for constraint in sa.inspect(op.get_bind()).get_check_constraints("articles")
    }:
        op.drop_constraint("ck_articles_status", "articles", type_="check")
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("articles")}
    for name in (
        "published_stage_at",
        "indexed_at",
        "curated_at",
        "extracted_at",
        "fetch_started_at",
        "discovered_at",
        "status_error",
    ):
        if name in columns:
            op.drop_column("articles", name)
