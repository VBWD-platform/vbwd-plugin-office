"""Create office_node / office_document / office_version / office_usage (S147-1).

ADDITIVE ONLY. Anchors on the plugin's own root revision
(``20260727_office_initial``), never on another plugin's chain (S147-00
already established the plugin's own migration graph).

One tree table (``office_node``, ``kind`` discriminated folder|document) per
the epic's D2 — a Doc/Sheet is a file in Space the moment it exists, so move
/ rename / trash / path are written once. ``office_document`` carries the
document-only facts, including ``sealed_data_key`` — the per-document
envelope-encryption data key (D3), sealed with the app secret and opened only
by ``DocumentStore``; nothing above the store ever sees it. ``office_version``
is deliberately append-only (no ``updated_at``/optimistic-lock column — a
version row is never mutated, only added) so restore is a pointer swap and a
CRDT can later be layered on without a migration. ``office_usage`` is a
materialised per-user byte total (O(1) quota reads) with ``user_id`` as its
own primary key — one row per user, no surrogate id needed.

Revision ID: 20260727_1200_office_space_tables
Revises: 20260727_office_initial
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260727_1200_office_space_tables"
down_revision = "20260727_office_initial"
branch_labels = None
depends_on = None

NODE_TABLE = "office_node"
DOCUMENT_TABLE = "office_document"
VERSION_TABLE = "office_version"
USAGE_TABLE = "office_usage"

NODE_KIND_LENGTH = 16
DOC_TYPE_LENGTH = 16
MIME_TYPE_LENGTH = 255
SHA256_HEX_LENGTH = 64
STORAGE_KEY_LENGTH = 512
NODE_NAME_LENGTH = 255


def upgrade():
    op.create_table(
        NODE_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=NODE_KIND_LENGTH), nullable=False),
        sa.Column("name", sa.String(length=NODE_NAME_LENGTH), nullable=False),
        sa.Column("trashed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["vbwd_user.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], [f"{NODE_TABLE}.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_office_node_owner_user_id", NODE_TABLE, ["owner_user_id"])
    op.create_index("ix_office_node_parent_id", NODE_TABLE, ["parent_id"])

    op.create_table(
        DOCUMENT_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("doc_type", sa.String(length=DOC_TYPE_LENGTH), nullable=False),
        sa.Column("mime_type", sa.String(length=MIME_TYPE_LENGTH), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Per-document envelope-encryption data key (D3), sealed with the app
        # secret. Opened only inside ``DocumentStore`` — everything above the
        # store sees plaintext bytes and never learns a key exists.
        sa.Column("sealed_data_key", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], [f"{NODE_TABLE}.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("node_id", name="uq_office_document_node_id"),
    )

    op.create_table(
        VERSION_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=STORAGE_KEY_LENGTH), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(length=SHA256_HEX_LENGTH), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], [f"{DOCUMENT_TABLE}.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "document_id", "version_no", name="uq_office_version_document_version_no"
        ),
    )
    op.create_index("ix_office_version_document_id", VERSION_TABLE, ["document_id"])

    op.create_table(
        USAGE_TABLE,
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bytes_used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["vbwd_user.id"], ondelete="CASCADE"),
    )


def downgrade():
    op.drop_table(USAGE_TABLE)
    op.drop_index("ix_office_version_document_id", table_name=VERSION_TABLE)
    op.drop_table(VERSION_TABLE)
    op.drop_table(DOCUMENT_TABLE)
    op.drop_index("ix_office_node_parent_id", table_name=NODE_TABLE)
    op.drop_index("ix_office_node_owner_user_id", table_name=NODE_TABLE)
    op.drop_table(NODE_TABLE)
