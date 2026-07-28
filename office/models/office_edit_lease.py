"""OfficeEditLease — a soft single-writer lock over one Doc node (S147-3,
epic D4).

Deliberately NOT a CRDT and NOT a ``BaseModel``: one row per node, upserted
on acquire/heartbeat and deleted on release (mirrors ``OfficeUsage`` — a
materialised, mutable, single-row-per-key state, not an append-only history).
The content model itself stays append-only-versioned specifically so a CRDT
can be layered on top of this later WITHOUT a migration — a lease is the
affordable MVP concurrency control in the meantime (one person edits,
others view; a stale lease simply expires and the next save wins it back).

``holder_share_id`` is carried per the epic's spec shape
(``holder_user_id | holder_share_id``) but is unused in this slice: Doc
editing (as opposed to the generic vault's ``/public/<token>*`` byte
routes) is reached only through a session (owner or a NAMED share), never an
anonymous link — see ``OfficeDocEditorService`` module docstring. Keeping the
column now avoids a second migration if anonymous Doc co-editing is ever
built.
"""
from sqlalchemy.dialects.postgresql import UUID

from vbwd.extensions import db

#: Default lease lifetime (seconds) before it lapses and a second editor's
#: next acquire attempt silently becomes a take-over. Ops-tunable via the
#: plugin's own config (``edit_lease_seconds``), never hardcoded elsewhere.
DEFAULT_LEASE_SECONDS = 90


class OfficeEditLease(db.Model):  # type: ignore[name-defined]
    """The current editor of one Doc node, if any."""

    __tablename__ = "office_edit_lease"

    node_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("office_node.id", ondelete="CASCADE"),
        primary_key=True,
    )
    holder_user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=True,
    )
    holder_share_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("office_share.id", ondelete="SET NULL"),
        nullable=True,
    )
    acquired_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<OfficeEditLease(node_id='{self.node_id}', holder_user_id='{self.holder_user_id}')>"
