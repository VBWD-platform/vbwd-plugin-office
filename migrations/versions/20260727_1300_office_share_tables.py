"""Create office_share / office_share_access; widen office_version (S147-2).

ADDITIVE. Anchors on the plugin's own prior revision
(``20260727_1200_office_space_tables``), never on another plugin's chain.

``office_share`` is the single capability-grant table for both share kinds
(named-user and link, epic D5): ``token_hash`` is the sha256 of a
high-entropy token shown to the owner exactly once (mirrors
``ApiKeyService``, S52); ``password_hash`` is bcrypt, nullable. Revocation is
a field write (``revoked_at``), never a row delete, so ``office_share_access``
(the C6 audit trail, also created here) stays reconstructable.

``office_version.created_by_user_id`` is widened to nullable and
``created_by_share_id`` is added so a purely anonymous share edit (control
C4) can honestly record "no user, but this capability" rather than being
forced to attribute the write to somebody it wasn't.

Revision ID: 20260727_1300_office_share_tables
Revises: 20260727_1200_office_space_tables
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260727_1300_office_share_tables"
down_revision = "20260727_1200_office_space_tables"
branch_labels = None
depends_on = None

SHARE_TABLE = "office_share"
SHARE_ACCESS_TABLE = "office_share_access"
VERSION_TABLE = "office_version"
NODE_TABLE = "office_node"

PERMISSION_LENGTH = 16
TOKEN_HASH_LENGTH = 64
ACTION_LENGTH = 32
IP_HASH_LENGTH = 64


def upgrade():
    op.create_table(
        SHARE_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=TOKEN_HASH_LENGTH), nullable=False),
        sa.Column("permission", sa.String(length=PERMISSION_LENGTH), nullable=False),
        sa.Column("subject_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "allow_anonymous",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["node_id"], [f"{NODE_TABLE}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["vbwd_user.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["subject_user_id"], ["vbwd_user.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="uq_office_share_token_hash"),
    )
    op.create_index("ix_office_share_node_id", SHARE_TABLE, ["node_id"])
    op.create_index("ix_office_share_subject_user_id", SHARE_TABLE, ["subject_user_id"])
    op.create_index("ix_office_share_token_hash", SHARE_TABLE, ["token_hash"])

    op.create_table(
        SHARE_ACCESS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("share_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=ACTION_LENGTH), nullable=False),
        sa.Column("ip_hash", sa.String(length=IP_HASH_LENGTH), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["share_id"], [f"{SHARE_TABLE}.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_office_share_access_share_id", SHARE_ACCESS_TABLE, ["share_id"])
    op.create_index(
        "ix_office_share_access_occurred_at", SHARE_ACCESS_TABLE, ["occurred_at"]
    )

    op.alter_column(
        VERSION_TABLE,
        "created_by_user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        VERSION_TABLE,
        sa.Column("created_by_share_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_office_version_created_by_share_id",
        VERSION_TABLE,
        SHARE_TABLE,
        ["created_by_share_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint(
        "fk_office_version_created_by_share_id", VERSION_TABLE, type_="foreignkey"
    )
    op.drop_column(VERSION_TABLE, "created_by_share_id")
    op.alter_column(
        VERSION_TABLE,
        "created_by_user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    op.drop_index("ix_office_share_access_occurred_at", table_name=SHARE_ACCESS_TABLE)
    op.drop_index("ix_office_share_access_share_id", table_name=SHARE_ACCESS_TABLE)
    op.drop_table(SHARE_ACCESS_TABLE)

    op.drop_index("ix_office_share_token_hash", table_name=SHARE_TABLE)
    op.drop_index("ix_office_share_subject_user_id", table_name=SHARE_TABLE)
    op.drop_index("ix_office_share_node_id", table_name=SHARE_TABLE)
    op.drop_table(SHARE_TABLE)
