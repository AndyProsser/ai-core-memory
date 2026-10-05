"""encrypted private memory

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

S = sqlmodel.sql.sqltypes.AutoString


def upgrade() -> None:
    with op.batch_alter_table("memory_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("encrypted", sa.Boolean(), server_default="0", nullable=False))
    with op.batch_alter_table("memory_revisions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("encrypted", sa.Boolean(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("key_user_id", S(), nullable=True))
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.add_column(sa.Column("wrapped_dek", S(), nullable=True))
    with op.batch_alter_table("oauth_codes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("wrapped_dek", S(), nullable=True))
    with op.batch_alter_table("oauth_grants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("wrapped_dek", S(), nullable=True))
    op.create_table(
        "user_keys",
        sa.Column("user_id", S(), nullable=False),
        sa.Column("kdf_salt", S(), nullable=False),
        sa.Column("kdf_time", sa.Integer(), nullable=False),
        sa.Column("kdf_memory_kib", sa.Integer(), nullable=False),
        sa.Column("kdf_parallelism", sa.Integer(), nullable=False),
        sa.Column("wrapped_by_passphrase", S(), nullable=False),
        sa.Column("wrapped_by_recovery", S(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("passphrase_changed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_keys")
    for table, cols in (
        ("oauth_grants", ["wrapped_dek"]),
        ("oauth_codes", ["wrapped_dek"]),
        ("api_tokens", ["wrapped_dek"]),
        ("memory_revisions", ["key_user_id", "encrypted"]),
        ("memory_records", ["encrypted"]),
    ):
        with op.batch_alter_table(table, schema=None) as batch_op:
            for c in cols:
                batch_op.drop_column(c)
