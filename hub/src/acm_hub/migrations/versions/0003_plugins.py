"""plugins

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("scope", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("project_slug", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("team_slug", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("owner_user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("instance_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("origin_instance_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_events_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_events_type"), ["type"], unique=False)

    op.create_table(
        "plugin_instances",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("plugin_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("owner_user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("secret_refs", sa.JSON(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("projects", sa.JSON(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("egress", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("user_scope_ack", sa.Boolean(), nullable=False),
        sa.Column("pull_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_digest_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("plugin_instances", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_plugin_instances_plugin_key"), ["plugin_key"], unique=False)

    op.create_table(
        "plugin_deliveries",
        sa.Column("event_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("instance_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["plugin_instances.id"],
        ),
        sa.PrimaryKeyConstraint("event_id", "instance_id"),
    )
    with op.batch_alter_table("plugin_deliveries", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_plugin_deliveries_next_attempt_at"), ["next_attempt_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("plugin_deliveries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_plugin_deliveries_next_attempt_at"))

    op.drop_table("plugin_deliveries")
    with op.batch_alter_table("plugin_instances", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_plugin_instances_plugin_key"))

    op.drop_table("plugin_instances")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_events_type"))
        batch_op.drop_index(batch_op.f("ix_events_created_at"))

    op.drop_table("events")
