"""mcp oauth

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

S = sqlmodel.sql.sqltypes.AutoString


def upgrade() -> None:
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.add_column(sa.Column("grant_id", S(), nullable=True))
        batch_op.create_index(batch_op.f("ix_api_tokens_grant_id"), ["grant_id"], unique=False)

    op.create_table(
        "oauth_clients",
        sa.Column("id", S(), nullable=False),
        sa.Column("client_name", S(), nullable=True),
        sa.Column("redirect_uris", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "oauth_requests",
        sa.Column("id", S(), nullable=False),
        sa.Column("client_id", S(), nullable=False),
        sa.Column("redirect_uri", S(), nullable=False),
        sa.Column("redirect_uri_provided_explicitly", sa.Boolean(), nullable=False),
        sa.Column("state", S(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("code_challenge", S(), nullable=False),
        sa.Column("resource", S(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("oauth_requests", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_oauth_requests_client_id"), ["client_id"], unique=False)

    op.create_table(
        "oauth_codes",
        sa.Column("code_hash", S(), nullable=False),
        sa.Column("client_id", S(), nullable=False),
        sa.Column("user_id", S(), nullable=False),
        sa.Column("redirect_uri", S(), nullable=False),
        sa.Column("redirect_uri_provided_explicitly", sa.Boolean(), nullable=False),
        sa.Column("code_challenge", S(), nullable=False),
        sa.Column("resource", S(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("project_ids", sa.JSON(), nullable=False),
        sa.Column("include_user_scope", sa.Boolean(), nullable=False),
        sa.Column("access_level", S(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("grant_id", S(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    with op.batch_alter_table("oauth_codes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_oauth_codes_client_id"), ["client_id"], unique=False)

    op.create_table(
        "oauth_grants",
        sa.Column("id", S(), nullable=False),
        sa.Column("user_id", S(), nullable=False),
        sa.Column("client_id", S(), nullable=False),
        sa.Column("project_ids", sa.JSON(), nullable=False),
        sa.Column("include_user_scope", sa.Boolean(), nullable=False),
        sa.Column("access_level", S(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("resource", S(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("refresh_hash", S(), nullable=False),
        sa.Column("prev_refresh_hash", S(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("oauth_grants", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_oauth_grants_client_id"), ["client_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_oauth_grants_prev_refresh_hash"), ["prev_refresh_hash"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_oauth_grants_refresh_hash"), ["refresh_hash"], unique=True)
        batch_op.create_index(batch_op.f("ix_oauth_grants_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_table("oauth_grants")
    op.drop_table("oauth_codes")
    op.drop_table("oauth_requests")
    op.drop_table("oauth_clients")
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_tokens_grant_id"))
        batch_op.drop_column("grant_id")
