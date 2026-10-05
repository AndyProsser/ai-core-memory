"""search plugin index bookkeeping

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

S = sqlmodel.sql.sqltypes.AutoString


def upgrade() -> None:
    op.create_table(
        "search_index_entries",
        sa.Column("instance_id", S(), nullable=False),
        sa.Column("record_id", S(), nullable=False),
        sa.Column("content_hash", S(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["instance_id"], ["plugin_instances.id"]),
        sa.PrimaryKeyConstraint("instance_id", "record_id"),
    )


def downgrade() -> None:
    op.drop_table("search_index_entries")
