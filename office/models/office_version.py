"""OfficeVersion — append-only version history for one document (S147-1).

Deliberately NOT a ``BaseModel``: a version row is never mutated once
written, only added, so it carries no ``updated_at`` and no optimistic-lock
``version`` column (those imply mutability this table intentionally does not
have). This is the domain reason restore is a pointer swap (a new row whose
``storage_key`` can point at an EXISTING blob) rather than a content
rewrite — and the reason a CRDT can later be layered onto Docs/Sheets without
a migration (see the epic's "Why append-only versions").

S147-2 (sharing) widens ``created_by_user_id`` to nullable and adds
``created_by_share_id``: a purely anonymous edit through an ``allow_anonymous``
link has no user identity at all, and control C4 requires that honesty be
visible in the version history rather than papered over with the owner's id.
A version created by a logged-in stranger via a share still records their
real ``created_by_user_id`` (we DO know who they were); only the fully
anonymous case leaves ``created_by_user_id`` NULL and points
``created_by_share_id`` at the capability that authorised the write instead.
"""
from uuid import uuid4

from sqlalchemy.dialects.postgresql import UUID

from vbwd.extensions import db
from vbwd.utils.datetime_utils import utcnow

STORAGE_KEY_MAX_LENGTH = 512
SHA256_HEX_LENGTH = 64


class OfficeVersion(db.Model):  # type: ignore[name-defined]
    """One immutable version of one document's content."""

    __tablename__ = "office_version"
    __table_args__ = (
        db.UniqueConstraint(
            "document_id", "version_no", name="uq_office_version_document_version_no"
        ),
    )

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("office_document.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_no = db.Column(db.Integer, nullable=False)
    storage_key = db.Column(db.String(STORAGE_KEY_MAX_LENGTH), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    sha256 = db.Column(db.String(SHA256_HEX_LENGTH), nullable=False)
    # Nullable (S147-2): NULL means "a purely anonymous share edit" — see the
    # module docstring. Not a FK: mirrors the rest of this table, which
    # records ids without enforcing referential integrity against the user
    # table (a deleted user must not cascade-delete version history).
    created_by_user_id = db.Column(UUID(as_uuid=True), nullable=True)
    # Set only for a write made through a share capability (S147-2 C4);
    # NULL for every ordinary owner-authenticated write.
    created_by_share_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("office_share.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def to_dict(self) -> dict:
        """Public projection — deliberately WITHOUT the raw ``storage_key``
        (mirrors ``DatasetSnapshotFile``: bytes are reached only through the
        entitlement-gated download route, never the raw location)."""
        return {
            "id": str(self.id),
            "document_id": str(self.document_id),
            "version_no": self.version_no,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_by_user_id": (
                str(self.created_by_user_id) if self.created_by_user_id else None
            ),
            "created_by_share_id": (
                str(self.created_by_share_id) if self.created_by_share_id else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<OfficeVersion(document_id='{self.document_id}', "
            f"version_no={self.version_no})>"
        )
