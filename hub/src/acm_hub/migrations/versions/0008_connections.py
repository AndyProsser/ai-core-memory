"""per-user connections

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("plugin_instances", schema=None) as batch_op:
        batch_op.add_column(sa.Column("personal", sa.Boolean(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("sealed_secrets", sa.JSON(), server_default="{}", nullable=False))
        batch_op.create_index(batch_op.f("ix_plugin_instances_personal"), ["personal"], unique=False)
    with op.batch_alter_table("instance_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("connections_enabled", sa.Boolean(), server_default="1", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("instance_settings", schema=None) as batch_op:
        batch_op.drop_column("connections_enabled")
    with op.batch_alter_table("plugin_instances", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_plugin_instances_personal"))
        batch_op.drop_column("sealed_secrets")
        batch_op.drop_column("personal")
