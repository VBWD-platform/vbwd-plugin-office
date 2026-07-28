"""OfficeShareAccess — the C6 audit trail for share resolution (S147-2).

Deliberately NOT a ``BaseModel``: an access row is append-only (written once,
never mutated), so it carries no ``updated_at`` / optimistic-lock column —
mirrors ``OfficeVersion``'s reasoning exactly. One row per share resolution
(metadata read, content read, content write, or a password unlock attempt) so
an owner can see how their link was used. ``ip_hash`` — never the raw IP — is
a proportionate, GDPR-conscious record (a hash is enough to spot abuse
patterns without storing a directly-identifying value); retention is bounded
by the plugin's ``share_access_log_retention_days`` config.
"""
from uuid import uuid4

from sqlalchemy.dialects.postgresql import UUID

from vbwd.extensions import db
from vbwd.utils.datetime_utils import utcnow

ACTION_RESOLVE = "resolve"
ACTION_UNLOCK = "unlock"
ACTION_CONTENT_READ = "content_read"
ACTION_CONTENT_WRITE = "content_write"
ALLOWED_ACTIONS = (
    ACTION_RESOLVE,
    ACTION_UNLOCK,
    ACTION_CONTENT_READ,
    ACTION_CONTENT_WRITE,
)

ACTION_MAX_LENGTH = 32
IP_HASH_LENGTH = 64  # sha256 hex digest


class OfficeShareAccess(db.Model):  # type: ignore[name-defined]
    """One immutable record of one share being resolved/used."""

    __tablename__ = "office_share_access"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    share_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("office_share.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action = db.Column(db.String(ACTION_MAX_LENGTH), nullable=False)
    ip_hash = db.Column(db.String(IP_HASH_LENGTH), nullable=True)
    occurred_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "share_id": str(self.share_id),
            "action": self.action,
            "ip_hash": self.ip_hash,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<OfficeShareAccess(share_id='{self.share_id}', action='{self.action}')>"
        )
