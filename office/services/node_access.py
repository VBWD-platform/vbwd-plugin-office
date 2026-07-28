"""``OfficeNodeAccess`` — owner-scoped node lookups shared by the document
service (S147-1).

Single reason to change: resolving a node id to an owned :class:`OfficeNode`
(or refusing to, indistinguishably from "does not exist" — test #9) and
guarding the tree against cycles on move. Extracted from
``OfficeDocumentService`` so that service stays focused on document/version
orchestration (SRP) and so a later slice (S147-2 sharing) can reuse the same
lookup without duplicating it (DRY).
"""
from typing import Optional

from plugins.office.office.models.office_node import NODE_KIND_FOLDER, OfficeNode
from plugins.office.office.services.exceptions import (
    OfficeInvalidNodeError,
    OfficeNodeNotFoundError,
)


class OfficeNodeAccess:
    def __init__(self, node_repository) -> None:
        self._node_repository = node_repository

    def require_owned_node(self, owner_user_id, node_id) -> OfficeNode:
        node = self._node_repository.find_by_id_for_owner(node_id, owner_user_id)
        if node is None:
            raise OfficeNodeNotFoundError(node_id)
        return node

    def resolve_parent_folder(self, owner_user_id, parent_id) -> Optional[OfficeNode]:
        """Return the owned folder ``parent_id`` names, or ``None`` when
        ``parent_id`` is falsy (root). Raises :class:`OfficeNodeNotFoundError`
        for a missing/foreign/non-folder/trashed parent — existence is never
        disclosed to a non-owner."""
        if not parent_id:
            return None
        parent = self.require_owned_node(owner_user_id, parent_id)
        if parent.kind != NODE_KIND_FOLDER or parent.trashed_at is not None:
            raise OfficeNodeNotFoundError(parent_id)
        return parent

    def guard_against_cycle(self, node: OfficeNode, new_parent: OfficeNode) -> None:
        """Raise :class:`OfficeInvalidNodeError` when moving ``node`` under
        ``new_parent`` would create a cycle (moving into itself or into one
        of its own descendants)."""
        if new_parent.id == node.id:
            raise OfficeInvalidNodeError("A node cannot be moved into itself")
        visited = set()
        current = new_parent
        while current.parent_id is not None and current.parent_id not in visited:
            if current.parent_id == node.id:
                raise OfficeInvalidNodeError(
                    "A folder cannot be moved into its own descendant"
                )
            visited.add(current.parent_id)
            current = self._node_repository.find_by_id(current.parent_id)
            if current is None:
                break
