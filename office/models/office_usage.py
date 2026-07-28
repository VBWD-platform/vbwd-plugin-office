"""OfficeUsage — materialised per-user storage total (S147-1 B2).

A single row per user (``user_id`` is its own primary key — no surrogate
``id`` needed) so a quota check is O(1) instead of summing
``office_version.size_bytes`` on every request.
"""
from sqlalchemy.dialects.postgresql import UUID

from vbwd.extensions import db
from vbwd.utils.datetime_utils import utcnow


class OfficeUsage(db.Model):  # type: ignore[name-defined]
    """One user's total bytes stored across every version blob they own."""

    __tablename__ = "office_usage"

    user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    bytes_used = db.Column(db.BigInteger, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def to_dict(self) -> dict:
        return {
            "user_id": str(self.user_id),
            "bytes_used": self.bytes_used,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<OfficeUsage(user_id='{self.user_id}', bytes_used={self.bytes_used})>"
