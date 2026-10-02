"""lifecycle: proposals, reinforcements, settings

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proposals",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("kind", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("target_ids", sa.JSON(), nullable=False),
        sa.Column("rationale", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("generated_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("generated_by_token_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("dedupe_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_by_user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("decided_by_label", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decision_note", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["generated_by_token_id"],
            ["api_tokens.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_proposals_dedupe_key"), ["dedupe_key"], unique=False)

    op.create_table(
        "reinforcements",
        sa.Column("record_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("source_ref", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("reinforced_at", sa.DateTime(), nullable=False),
        sa.Column("by_token_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("by_user_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(
            ["by_token_id"],
            ["api_tokens.id"],
        ),
        sa.ForeignKeyConstraint(
            ["by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["memory_records.id"],
        ),
        sa.PrimaryKeyConstraint("record_id", "source_ref"),
    )
    with op.batch_alter_table("instance_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("stale_after_days_observed", sa.Integer(), server_default="90", nullable=False)
        )
        batch_op.add_column(
            sa.Column("stale_after_days_confirmed", sa.Integer(), server_default="365", nullable=False)
        )
        batch_op.add_column(
            sa.Column("review_established_days", sa.Integer(), server_default="365", nullable=False)
        )
        batch_op.add_column(
            sa.Column("auto_apply_proposals", sa.Boolean(), server_default="0", nullable=False)
        )
        batch_op.add_column(sa.Column("last_consolidation_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("instance_settings", schema=None) as batch_op:
        batch_op.drop_column("last_consolidation_at")
        batch_op.drop_column("auto_apply_proposals")
        batch_op.drop_column("review_established_days")
        batch_op.drop_column("stale_after_days_confirmed")
        batch_op.drop_column("stale_after_days_observed")

    op.drop_table("reinforcements")
    with op.batch_alter_table("proposals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_proposals_dedupe_key"))

    op.drop_table("proposals")
