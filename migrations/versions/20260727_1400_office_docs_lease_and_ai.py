"""Create office_edit_lease / office_ai_call; add office_document.ai_enabled (S147-3).

ADDITIVE. Anchors on the plugin's own prior revision
(``20260727_1300_office_share_tables``), never on another plugin's chain.

``office_edit_lease`` is the epic D4 soft single-writer lock: one row per
node (``node_id`` is its own primary key, upserted on acquire/heartbeat,
deleted on release/expiry — mirrors ``office_usage``'s one-row-per-key
shape). ``office_ai_call`` is the S147-3 AI-helper audit trail + the read
side of the per-user monthly call budget. ``office_document.ai_enabled`` is
the per-document opt-in for the AI helper, defaulting OFF for every existing
and future document (the owner decides whether content ever leaves the box).

Revision ID: 20260727_1400_office_docs_lease_and_ai
Revises: 20260727_1300_office_share_tables
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260727_1400_office_docs_lease_and_ai"
down_revision = "20260727_1300_office_share_tables"
branch_labels = None
depends_on = None

DOCUMENT_TABLE = "office_document"
NODE_TABLE = "office_node"
SHARE_TABLE = "office_share"
LEASE_TABLE = "office_edit_lease"
AI_CALL_TABLE = "office_ai_call"

CAPABILITY_LENGTH = 32
CONNECTION_SLUG_LENGTH = 128
STATUS_LENGTH = 16


def upgrade():
    op.add_column(
        DOCUMENT_TABLE,
        sa.Column(
            "ai_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )

    op.create_table(
        LEASE_TABLE,
        sa.Column("node_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("holder_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("holder_share_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], [f"{NODE_TABLE}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["holder_user_id"], ["vbwd_user.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["holder_share_id"], [f"{SHARE_TABLE}.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_office_edit_lease_expires_at", LEASE_TABLE, ["expires_at"])

    op.create_table(
        AI_CALL_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("capability", sa.String(length=CAPABILITY_LENGTH), nullable=False),
        sa.Column(
            "connection_slug", sa.String(length=CONNECTION_SLUG_LENGTH), nullable=False
        ),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=STATUS_LENGTH),
            nullable=False,
            server_default="success",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], [f"{NODE_TABLE}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["vbwd_user.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_office_ai_call_node_id", AI_CALL_TABLE, ["node_id"])
    op.create_index("ix_office_ai_call_user_id", AI_CALL_TABLE, ["user_id"])
    op.create_index("ix_office_ai_call_created_at", AI_CALL_TABLE, ["created_at"])


def downgrade():
    op.drop_index("ix_office_ai_call_created_at", table_name=AI_CALL_TABLE)
    op.drop_index("ix_office_ai_call_user_id", table_name=AI_CALL_TABLE)
    op.drop_index("ix_office_ai_call_node_id", table_name=AI_CALL_TABLE)
    op.drop_table(AI_CALL_TABLE)

    op.drop_index("ix_office_edit_lease_expires_at", table_name=LEASE_TABLE)
    op.drop_table(LEASE_TABLE)

    op.drop_column(DOCUMENT_TABLE, "ai_enabled")
