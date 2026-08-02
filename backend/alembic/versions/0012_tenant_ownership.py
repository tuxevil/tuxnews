"""Make aggregate membership rows tenant-aware."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_tenant_ownership"
down_revision: str | None = "0011_user_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _foreign_keys(table_name: str) -> set[str]:
    return {
        foreign_key["name"]
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(table_name)
        if foreign_key.get("name")
    }


def _add_tenant_column(table_name: str, parent_table: str, parent_column: str) -> None:
    columns = _columns(table_name)
    if "user_id" not in columns:
        op.add_column(table_name, sa.Column("user_id", sa.Integer(), nullable=True))
        op.execute(
            sa.text(
                f"UPDATE {table_name} SET user_id = "
                f"(SELECT user_id FROM {parent_table} WHERE {parent_table}.id = {table_name}.{parent_column}) "
                "WHERE user_id IS NULL"
            )
        )
        op.alter_column(table_name, "user_id", nullable=False)
    index_name = f"ix_{table_name}_user_id"
    if index_name not in _indexes(table_name):
        op.create_index(index_name, table_name, ["user_id"])
    foreign_key_name = f"fk_{table_name}_user_id_users"
    if op.get_bind().dialect.name != "sqlite" and foreign_key_name not in _foreign_keys(table_name):
        op.create_foreign_key(foreign_key_name, table_name, "users", ["user_id"], ["id"], ondelete="CASCADE")


def upgrade() -> None:
    _add_tenant_column("cluster_members", "clusters", "cluster_id")
    _add_tenant_column("briefing_items", "briefings", "briefing_id")


def downgrade() -> None:
    for table_name in ("briefing_items", "cluster_members"):
        foreign_key_name = f"fk_{table_name}_user_id_users"
        if op.get_bind().dialect.name != "sqlite" and foreign_key_name in _foreign_keys(table_name):
            op.drop_constraint(foreign_key_name, table_name, type_="foreignkey")
        index_name = f"ix_{table_name}_user_id"
        if index_name in _indexes(table_name):
            op.drop_index(index_name, table_name=table_name)
        if "user_id" in _columns(table_name):
            op.drop_column(table_name, "user_id")
