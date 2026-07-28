"""OfficeDocument — document-only facts for an ``office_node`` of kind
``document`` (S147-1).

Carries ``sealed_data_key``: the per-document envelope-encryption data key
(epic D3), sealed with the app secret. It is opened only inside
``DocumentStore`` — the repository/service/route layers above it read and
write plaintext bytes and never learn a key exists (never surfaced by
``to_dict``).

``ai_enabled`` (S147-3) is the owner's opt-in for the AI helper on THIS
document — defaults ``False`` for every document, never flipped on by a
share. Only the owner may change it (enforced in ``OfficeDocEditorService``,
not here); the AI route 403s whenever it is off, regardless of the caller's
otherwise-sufficient permission — the honest half of epic D3 (server-side
encryption, not E2EE): the owner decides whether THIS content ever leaves
the box for an LLM call.
"""
from sqlalchemy.dialects.postgresql import UUID

from vbwd.extensions import db
from vbwd.models.base import BaseModel

DOC_TYPE_FILE = "file"
DOC_TYPE_TEXT = "text"
DOC_TYPE_SHEET = "sheet"
ALLOWED_DOC_TYPES = (DOC_TYPE_FILE, DOC_TYPE_TEXT, DOC_TYPE_SHEET)

MIME_TYPE_MAX_LENGTH = 255


class OfficeDocument(BaseModel):
    """Document facts for one ``office_node`` (one-to-one via ``node_id``)."""

    __tablename__ = "office_document"

    node_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("office_node.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    doc_type = db.Column(db.String(16), nullable=False, default=DOC_TYPE_FILE)
    mime_type = db.Column(db.String(MIME_TYPE_MAX_LENGTH), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    current_version_id = db.Column(UUID(as_uuid=True), nullable=True)
    sealed_data_key = db.Column(db.Text, nullable=False)
    ai_enabled = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self) -> dict:
        """Public projection — deliberately WITHOUT ``sealed_data_key``."""
        return {
            "id": str(self.id),
            "node_id": str(self.node_id),
            "doc_type": self.doc_type,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "ai_enabled": self.ai_enabled,
            "current_version_id": (
                str(self.current_version_id) if self.current_version_id else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<OfficeDocument(node_id='{self.node_id}', doc_type='{self.doc_type}')>"
