"""Create the empty initial revision.

Revision ID: 0000_base
Revises:
"""

from collections.abc import Sequence

revision: str = "0000_base"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
