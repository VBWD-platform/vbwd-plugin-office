"""``OfficeNodeRepository`` — data access for the vault tree (S147-1).

``find_by_id_for_owner`` is the ONE query the routes use to resolve a node —
filtering on ``id`` AND ``owner_user_id`` together in a single query means a
node that exists but belongs to someone else returns exactly the same
``None`` as a node that does not exist at all, so the caller cannot
distinguish the two (test #9: another user's node id -> 404, never 403).
"""
from typing import List, Optional

from plugins.office.office.models.office_node import OfficeNode


class OfficeNodeRepository:
    def __init__(self, session) -> None:
        self.session = session

    def add(self, node: OfficeNode) -> OfficeNode:
        self.session.add(node)
        self.session.flush()
        return node

    def delete(self, node: OfficeNode) -> None:
        self.session.delete(node)
        self.session.flush()

    def find_by_id(self, node_id) -> Optional[OfficeNode]:
        """Owner-UNSCOPED lookup — only for internal ancestry walks (cycle
        detection on move) where the owner has already been proven by the
        caller. Never used to answer a request directly."""
        return self.session.query(OfficeNode).filter(OfficeNode.id == node_id).first()

    def find_by_id_for_owner(self, node_id, owner_user_id) -> Optional[OfficeNode]:
        return (
            self.session.query(OfficeNode)
            .filter(OfficeNode.id == node_id, OfficeNode.owner_user_id == owner_user_id)
            .first()
        )

    def find_children(
        self, owner_user_id, parent_id, include_trashed: bool = False
    ) -> List[OfficeNode]:
        query = self.session.query(OfficeNode).filter(
            OfficeNode.owner_user_id == owner_user_id,
            OfficeNode.parent_id == parent_id,
        )
        if not include_trashed:
            query = query.filter(OfficeNode.trashed_at.is_(None))
        return query.order_by(OfficeNode.name.asc()).all()
