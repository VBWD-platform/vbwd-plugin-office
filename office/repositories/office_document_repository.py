"""``OfficeDocumentRepository`` — data access for document facts (S147-1).

``list_for_admin`` (added for the admin catalogue, S147 gap-fix) is the one
query that joins across ``office_document`` / ``office_node`` / the core
``vbwd_user`` / ``office_version`` — kept here rather than duplicated at the
route layer (DRY), and deliberately returns ONLY the metadata columns the
admin screen needs (id, owner id+email, name, type, size, timestamps, version
count) — never content, never a share token, never anything that would let
an admin read a user's file THROUGH this listing (the admin surface's GDPR
constraint).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func

from vbwd.models.user import User

from plugins.office.office.models.office_document import OfficeDocument
from plugins.office.office.models.office_node import OfficeNode
from plugins.office.office.models.office_version import OfficeVersion


@dataclass(frozen=True)
class AdminDocumentRow:
    """One admin-catalogue row — metadata only (GDPR: no content, no token)."""

    id: UUID
    owner_user_id: UUID
    owner_email: str
    name: str
    doc_type: str
    mime_type: str
    size_bytes: int
    version_count: int
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "owner": {"id": str(self.owner_user_id), "email": self.owner_email},
            "name": self.name,
            "doc_type": self.doc_type,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "version_count": self.version_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class OfficeDocumentRepository:
    def __init__(self, session) -> None:
        self.session = session

    def add(self, document: OfficeDocument) -> OfficeDocument:
        self.session.add(document)
        self.session.flush()
        return document

    def find_by_id(self, document_id) -> Optional[OfficeDocument]:
        return (
            self.session.query(OfficeDocument)
            .filter(OfficeDocument.id == document_id)
            .first()
        )

    def count_all(self) -> int:
        """Total documents across every owner — the ``/meta`` probe's count."""
        return self.session.query(OfficeDocument).count()

    def find_by_node_id(self, node_id) -> Optional[OfficeDocument]:
        return (
            self.session.query(OfficeDocument)
            .filter(OfficeDocument.node_id == node_id)
            .first()
        )

    def list_for_admin(
        self, page: int, per_page: int
    ) -> Tuple[List[AdminDocumentRow], int]:
        """Return ``(rows, total)`` — every document across every owner,
        newest-updated first, joined to its owner's email and its version
        count. Trashed nodes are included (the admin screen is an operator
        audit surface, not the end-user vault view)."""
        version_counts = (
            self.session.query(
                OfficeVersion.document_id,
                func.count(OfficeVersion.id).label("version_count"),
            )
            .group_by(OfficeVersion.document_id)
            .subquery()
        )

        base_query = (
            self.session.query(
                OfficeDocument,
                OfficeNode,
                User.email,
                version_counts.c.version_count,
            )
            .join(OfficeNode, OfficeNode.id == OfficeDocument.node_id)
            .join(User, User.id == OfficeNode.owner_user_id)
            .outerjoin(
                version_counts, version_counts.c.document_id == OfficeDocument.id
            )
        )

        total = base_query.count()
        rows = (
            base_query.order_by(OfficeNode.updated_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        return (
            [
                AdminDocumentRow(
                    id=document.id,
                    owner_user_id=node.owner_user_id,
                    owner_email=owner_email,
                    name=node.name,
                    doc_type=document.doc_type,
                    mime_type=document.mime_type,
                    size_bytes=document.size_bytes,
                    version_count=version_count or 0,
                    created_at=node.created_at,
                    updated_at=node.updated_at,
                )
                for document, node, owner_email, version_count in rows
            ],
            total,
        )
