"""``OfficeShareAccessRepository`` — data access for the C6 audit trail
(S147-2)."""
from typing import List

from plugins.office.office.models.office_share_access import OfficeShareAccess


class OfficeShareAccessRepository:
    def __init__(self, session) -> None:
        self.session = session

    def add(self, access: OfficeShareAccess) -> OfficeShareAccess:
        self.session.add(access)
        self.session.flush()
        return access

    def find_by_share_id(self, share_id) -> List[OfficeShareAccess]:
        return (
            self.session.query(OfficeShareAccess)
            .filter(OfficeShareAccess.share_id == share_id)
            .order_by(OfficeShareAccess.occurred_at.desc())
            .all()
        )

    def delete_older_than(self, cutoff) -> int:
        """Retention prune (C6's "retention window from config"). Returns the
        number of rows deleted. Not wired to a scheduler in this slice — an
        operator/CLI can call it; see the plugin config's
        ``share_access_log_retention_days``."""
        deleted = (
            self.session.query(OfficeShareAccess)
            .filter(OfficeShareAccess.occurred_at < cutoff)
            .delete(synchronize_session=False)
        )
        return deleted
