"""OfficeNode — the vault's tree (S147-1).

One table for both folders and documents (``kind`` discriminated), per the
epic's D2: a Doc/Sheet is a file in Space the moment it exists, so move /
rename / trash / path are written once instead of duplicated per document
kind. Owner-only in this slice (S147-1); sharing is a later slice (S147-2).
"""
from sqlalchemy.dialects.postgresql import UUID

from vbwd.extensions import db
from vbwd.models.base import BaseModel

NODE_NAME_MAX_LENGTH = 255

NODE_KIND_FOLDER = "folder"
NODE_KIND_DOCUMENT = "document"
ALLOWED_NODE_KINDS = (NODE_KIND_FOLDER, NODE_KIND_DOCUMENT)


class OfficeNode(BaseModel):
    """A folder or a document in one user's vault."""

    __tablename__ = "office_node"

    owner_user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("vbwd_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("office_node.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    kind = db.Column(db.String(16), nullable=False)
    name = db.Column(db.String(NODE_NAME_MAX_LENGTH), nullable=False)
    trashed_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "owner_user_id": str(self.owner_user_id),
            "parent_id": str(self.parent_id) if self.parent_id else None,
            "kind": self.kind,
            "name": self.name,
            "trashed_at": self.trashed_at.isoformat() if self.trashed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<OfficeNode(kind='{self.kind}', name='{self.name}')>"
