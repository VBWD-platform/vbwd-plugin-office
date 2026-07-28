"""``OfficeShareRepository`` — data access for share capability rows (S147-2).

``find_by_id_for_owner`` joins through ``office_node`` so an owner-side share
id that exists but belongs to someone else's document answers exactly the
same ``None`` as one that does not exist — mirrors
``OfficeNodeRepository.find_by_id_for_owner``'s existence-hiding contract
(test #9's reasoning, applied to shares)."""
from typing import List, Optional

from vbwd.utils.datetime_utils import utcnow

from plugins.office.office.models.office_node import OfficeNode
from plugins.office.office.models.office_share import OfficeShare


class OfficeShareRepository:
    def __init__(self, session) -> None:
        self.session = session

    def add(self, share: OfficeShare) -> OfficeShare:
        self.session.add(share)
        self.session.flush()
        return share

    def find_by_id(self, share_id) -> Optional[OfficeShare]:
        return (
            self.session.query(OfficeShare).filter(OfficeShare.id == share_id).first()
        )

    def find_by_id_for_owner(self, share_id, owner_user_id) -> Optional[OfficeShare]:
        return (
            self.session.query(OfficeShare)
            .join(OfficeNode, OfficeShare.node_id == OfficeNode.id)
            .filter(
                OfficeShare.id == share_id, OfficeNode.owner_user_id == owner_user_id
            )
            .first()
        )

    def find_by_node_id(self, node_id) -> List[OfficeShare]:
        return (
            self.session.query(OfficeShare)
            .filter(OfficeShare.node_id == node_id)
            .order_by(OfficeShare.created_at.desc())
            .all()
        )

    def find_by_token_hash(self, token_hash: str) -> Optional[OfficeShare]:
        return (
            self.session.query(OfficeShare)
            .filter(OfficeShare.token_hash == token_hash)
            .first()
        )

    def find_active_named_shares_for_user(self, user_id) -> List[OfficeShare]:
        """Live (not revoked, not expired) named shares for "Shared with
        me". Filtering revoked/expired here would duplicate
        ``AccessResolver``'s own policy — kept intentionally loose (revoked
        excluded, but expiry re-checked with a plain comparison, matching
        ``is_share_live``) since this list is a display convenience, not an
        access decision; the resolver is still the sole ACL authority."""
        now = utcnow()
        return (
            self.session.query(OfficeShare)
            .filter(
                OfficeShare.subject_user_id == user_id,
                OfficeShare.revoked_at.is_(None),
            )
            .filter((OfficeShare.expires_at.is_(None)) | (OfficeShare.expires_at > now))
            .order_by(OfficeShare.created_at.desc())
            .all()
        )
