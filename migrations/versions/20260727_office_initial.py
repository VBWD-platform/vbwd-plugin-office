"""office (VBWD Office) — root revision, phase 0 (S147-00).

Own ROOT revision (``down_revision = None``) on purpose: anchoring a plugin
migration on another plugin's revision fragments the graph, and then
``alembic upgrade heads`` fails with a KeyError unless that exact plugin is
also cloned.

Phase 0 ships plumbing and empty states only — no document model yet (see the
epic's "Deliberately NOT in this phase") — so this revision creates no schema.
It exists purely to anchor the plugin's migration chain from day one so
S147-1's first real table has a parent revision to build on.

Revision ID: 20260727_office_initial
Create Date: 2026-07-27
"""

revision = "20260727_office_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """No schema in phase 0 — see module docstring."""
    pass


def downgrade():
    """No schema in phase 0 — see module docstring."""
    pass
