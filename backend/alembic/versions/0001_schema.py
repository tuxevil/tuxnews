"""Create the first relational schema.

Revision ID: 0001_schema
Revises: 0000_base
"""

from collections.abc import Sequence

from alembic import op
from app.db import models  # noqa: F401
from app.db.base import Base

revision: str = "0001_schema"
down_revision: str | None = "0000_base"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
