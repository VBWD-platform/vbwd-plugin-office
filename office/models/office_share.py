"""OfficeShare — a capability, not an identity (S147-2 epic D5).

One table for both share kinds (per the epic's "one table, one resolver"):

* **Named share** (``subject_user_id`` set) — the recipient's existing session
  identifies them; appears in their "Shared with me" view.
* **Link share** (``subject_user_id`` NULL) — the bearer of ``token_hash``'s
  plaintext gets the permission. ``allow_anonymous=True`` lets the bearer be
  logged out entirely; ``False`` still requires *some* logged-in account.

The token is shown to the owner exactly ONCE at creation time; only its
sha256 hash is persisted (``token_hash``), mirroring ``ApiKeyService`` (S52) —
a leaked database dump must not hand over a live capability. Same reasoning
for ``password_hash`` (bcrypt, via ``share_password.py``).

Revocation is a field write (``revoked_at``), not a row delete, so the audit
trail (``office_share_access``) and "who shared what with whom" stay
reconstructable even after a share is turned off (C7 — revocation must be
immediate and must never be undone by a lingering cache, but the row itself
persists for history).
"""
from sqlalchemy.dialects.postgresql import UUID

from vbwd.extensions import db
from vbwd.models.base import BaseModel

PERMISSION_VIEW = "view"
PERMISSION_COMMENT = "comment"
PERMISSION_EDIT = "edit"
ALLOWED_SHARE_PERMISSIONS = (PERMISSION_VIEW, PERMISSION_COMMENT, PERMISSION_EDIT)

#: Single source of truth for "most permissive share wins" (AccessResolver)
#: and "this operation needs at least X" (SharingService). Higher = stronger.
PERMISSION_RANK = {
    PERMISSION_VIEW: 1,
    PERMISSION_COMMENT: 2,
    PERMISSION_EDIT: 3,
}

TOKEN_HASH_LENGTH = 64  # sha256 hex digest


class OfficeShare(BaseModel):
    """One capability grant on one ``office_node`` (folder or document)."""

    __tablename__ = "office_share"

    node_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("office_node.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash = db.Column(
        db.String(TOKEN_HASH_LENGTH), nullable=False, unique=True, index=True
    )
    permission = db.Column(db.String(16), nullable=False)
    subject_user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    allow_anonymous = db.Column(db.Boolean, nullable=False, default=False)
    password_hash = db.Column(db.Text, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    last_used_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> dict:
        """Public projection — deliberately WITHOUT ``token_hash`` /
        ``password_hash``; the plaintext token is only ever returned once, at
        creation, by the route that calls ``SharingService.create_share``."""
        return {
            "id": str(self.id),
            "node_id": str(self.node_id),
            "created_by_user_id": str(self.created_by_user_id),
            "permission": self.permission,
            "subject_user_id": (
                str(self.subject_user_id) if self.subject_user_id else None
            ),
            "allow_anonymous": self.allow_anonymous,
            "has_password": self.password_hash is not None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "last_used_at": (
                self.last_used_at.isoformat() if self.last_used_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<OfficeShare(node_id='{self.node_id}', permission='{self.permission}')>"
        )
