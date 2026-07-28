"""``OfficeUsageRepository`` — data access for the materialised per-user
storage total (S147-1 B2)."""
from typing import List, Optional, Tuple
from uuid import UUID

from vbwd.models.user import User

from plugins.office.office.models.office_usage import OfficeUsage


class OfficeUsageRepository:
    def __init__(self, session) -> None:
        self.session = session

    def find_by_user_id(self, user_id) -> Optional[OfficeUsage]:
        return (
            self.session.query(OfficeUsage)
            .filter(OfficeUsage.user_id == user_id)
            .first()
        )

    def list_all_with_owner_email(self) -> List[Tuple[UUID, str, int]]:
        """Every user's materialised usage total joined to their email — the
        admin storage overview's per-user breakdown (GDPR: id + email only,
        never any document content or listing). Ordered highest-usage-first."""
        rows = (
            self.session.query(OfficeUsage.user_id, User.email, OfficeUsage.bytes_used)
            .join(User, User.id == OfficeUsage.user_id)
            .order_by(OfficeUsage.bytes_used.desc())
            .all()
        )
        return [(user_id, email, bytes_used) for user_id, email, bytes_used in rows]

    def increment(self, user_id, delta_bytes: int) -> OfficeUsage:
        """Add ``delta_bytes`` (may be negative) to the user's total,
        creating the row on first use. Never goes below zero."""
        usage = self.find_by_user_id(user_id)
        if usage is None:
            usage = OfficeUsage(user_id=user_id, bytes_used=0)
            self.session.add(usage)
        usage.bytes_used = max(0, usage.bytes_used + delta_bytes)
        self.session.flush()
        return usage
