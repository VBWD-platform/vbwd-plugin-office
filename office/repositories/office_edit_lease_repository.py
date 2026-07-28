"""``OfficeEditLeaseRepository`` — data access for the S147-3 edit lease.

One row per node (the primary key IS ``node_id``), so "acquire/heartbeat" is
an upsert and "release" is a delete — mirrors ``OfficeUsageRepository``'s
one-row-per-key shape.
"""
from typing import Optional

from plugins.office.office.models.office_edit_lease import OfficeEditLease


class OfficeEditLeaseRepository:
    def __init__(self, session) -> None:
        self.session = session

    def find_by_node_id(self, node_id) -> Optional[OfficeEditLease]:
        return (
            self.session.query(OfficeEditLease)
            .filter(OfficeEditLease.node_id == node_id)
            .first()
        )

    def upsert(
        self, node_id, holder_user_id, acquired_at, expires_at
    ) -> OfficeEditLease:
        lease = self.find_by_node_id(node_id)
        if lease is None:
            lease = OfficeEditLease(node_id=node_id)
            self.session.add(lease)
        lease.holder_user_id = holder_user_id
        lease.acquired_at = acquired_at
        lease.expires_at = expires_at
        self.session.flush()
        return lease

    def delete(self, node_id) -> None:
        lease = self.find_by_node_id(node_id)
        if lease is not None:
            self.session.delete(lease)
            self.session.flush()
