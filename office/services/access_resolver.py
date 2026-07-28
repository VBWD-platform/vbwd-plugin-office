"""``AccessResolver`` — ONE object every route asks for its ACL answer
(S147-2, epic D5 / the sprint's "one resolver, one answer").

Owner always wins; a folder share inherits to every descendant (walking the
node's own ancestry, never a descendant's); the most permissive matching
share wins; a revoked or expired share never matches. Two ACL code paths is
how one of them ends up wrong (DRY) — the owner-side routes and the public
``/public/<token>`` routes both call :meth:`resolve` (or
:meth:`resolve_share`, when the caller also needs the matched row, e.g. to
check ``password_hash``) and nothing else ever re-implements this logic.
"""
from __future__ import annotations

from typing import Optional, Tuple

from vbwd.utils.datetime_utils import utcnow

from plugins.office.office.models.office_share import PERMISSION_RANK
from plugins.office.office.services.share_token import hash_share_token, tokens_match

#: The four values :meth:`AccessResolver.resolve` can return (``None`` means
#: no access at all).
ACCESS_OWNER = "owner"

Access = Optional[str]


def is_share_live(share) -> bool:
    """True iff ``share`` is neither revoked nor expired (C7 — revocation is
    immediate: this check runs fresh on every resolution, there is no cache
    layer to outlive it)."""
    if share.revoked_at is not None:
        return False
    if share.expires_at is not None and share.expires_at <= utcnow():
        return False
    return True


class AccessResolver:
    def __init__(self, node_repository, share_repository) -> None:
        self._node_repository = node_repository
        self._share_repository = share_repository

    def resolve(
        self, node_id, *, user_id=None, share_token: Optional[str] = None
    ) -> Access:
        """Return ``'owner' | 'edit' | 'comment' | 'view' | None``."""
        resolved = self.resolve_share(node_id, user_id=user_id, share_token=share_token)
        return resolved[0] if resolved is not None else None

    def resolve_share(
        self, node_id, *, user_id=None, share_token: Optional[str] = None
    ) -> Optional[Tuple[str, Optional[object]]]:
        """Like :meth:`resolve`, but also returns the matched ``OfficeShare``
        row (``None`` for owner access) — callers that must additionally
        check e.g. ``password_hash`` use this instead of duplicating the
        walk. Returns ``None`` when there is no access at all."""
        node = self._node_repository.find_by_id(node_id)
        if node is None:
            return None
        if user_id is not None and node.owner_user_id == user_id:
            return ACCESS_OWNER, None

        presented_hash = hash_share_token(share_token) if share_token else None
        best_permission: Optional[str] = None
        best_share = None

        current = node
        while current is not None:
            for share in self._share_repository.find_by_node_id(current.id):
                if not is_share_live(share):
                    continue
                if not self._share_matches(
                    share, user_id=user_id, presented_hash=presented_hash
                ):
                    continue
                if (
                    best_permission is None
                    or PERMISSION_RANK[share.permission]
                    > PERMISSION_RANK[best_permission]
                ):
                    best_permission = share.permission
                    best_share = share
            current = (
                self._node_repository.find_by_id(current.parent_id)
                if current.parent_id
                else None
            )

        if best_permission is None:
            return None
        return best_permission, best_share

    @staticmethod
    def _share_matches(share, *, user_id, presented_hash: Optional[str]) -> bool:
        if share.subject_user_id is not None:
            # Named share — the recipient's own session identifies them; a
            # token, even the right one, is neither needed nor consulted.
            return user_id is not None and share.subject_user_id == user_id

        # Link share — the bearer of the token gets the permission.
        if presented_hash is None or not tokens_match(presented_hash, share.token_hash):
            return False
        return bool(share.allow_anonymous) or user_id is not None
