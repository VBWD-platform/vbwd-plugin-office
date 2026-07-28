"""``EditLeaseService`` — the S147-3 edit lease (epic D4): a soft
single-writer lock, not a CRDT.

Opening a Doc for editing acquires the lease (default 90s, config-tunable);
a heartbeat call renews it. A second editor's acquire attempt while a LIVE
lease is held by someone else is simply told "not granted" (read-only, with
whose lease it is and when it lapses) rather than erroring — the FE renders
that as the "X is editing" banner. Once the lease lapses, the very next
acquire call succeeds normally: THAT is the "take-over" action from the
sprint doc — there is no separate take-over endpoint, because an expired
lease offers no exclusivity to defend.

Presence (``GET .../presence``) is a poll target, not a push stream — the
sprint explicitly prefers polling here (a long-lived stream per open document
starved gunicorn once already, in meinchat); a 5s client-side poll is cheap
and this service's reads are O(1) single-row lookups.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from vbwd.utils.datetime_utils import utcnow

from plugins.office.office.models.office_edit_lease import DEFAULT_LEASE_SECONDS
from plugins.office.office.services.exceptions import OfficeDocLockedError


@dataclass(frozen=True)
class LeaseState:
    """The lease's current state, from one caller's point of view."""

    held: bool
    holder_user_id: Optional[object]
    is_self: bool
    granted: bool
    expires_at: Optional[datetime]

    def to_dict(self) -> dict:
        return {
            "held": self.held,
            "holder_user_id": str(self.holder_user_id) if self.holder_user_id else None,
            "is_self": self.is_self,
            "granted": self.granted,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


_UNHELD_STATE = LeaseState(
    held=False, holder_user_id=None, is_self=False, granted=True, expires_at=None
)


class EditLeaseService:
    def __init__(
        self, lease_repository, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> None:
        self._lease_repository = lease_repository
        self._lease_seconds = lease_seconds

    def acquire(self, node_id, holder_user_id) -> LeaseState:
        """Acquire, renew (same holder), or take over (lapsed) the lease.

        Returns ``granted=False`` — never raises — when a LIVE lease is held
        by someone else; the caller stays read-only.
        """
        lease = self._lease_repository.find_by_node_id(node_id)
        now = utcnow()
        if (
            lease is None
            or lease.expires_at <= now
            or lease.holder_user_id == holder_user_id
        ):
            expires_at = now + timedelta(seconds=self._lease_seconds)
            lease = self._lease_repository.upsert(
                node_id, holder_user_id, now, expires_at
            )
            return LeaseState(
                held=True,
                holder_user_id=holder_user_id,
                is_self=True,
                granted=True,
                expires_at=lease.expires_at,
            )
        return LeaseState(
            held=True,
            holder_user_id=lease.holder_user_id,
            is_self=False,
            granted=False,
            expires_at=lease.expires_at,
        )

    def release(self, node_id, holder_user_id) -> None:
        """No-op unless ``holder_user_id`` is the CURRENT holder — releasing
        a lease you do not hold must never affect someone else's session."""
        lease = self._lease_repository.find_by_node_id(node_id)
        if lease is not None and lease.holder_user_id == holder_user_id:
            self._lease_repository.delete(node_id)

    def current(self, node_id, requester_user_id) -> LeaseState:
        """Read-only snapshot for the presence poll and the doc-open payload."""
        lease = self._lease_repository.find_by_node_id(node_id)
        now = utcnow()
        if lease is None or lease.expires_at <= now:
            return _UNHELD_STATE
        is_self = lease.holder_user_id == requester_user_id
        return LeaseState(
            held=True,
            holder_user_id=lease.holder_user_id,
            is_self=is_self,
            granted=is_self,
            expires_at=lease.expires_at,
        )

    def assert_not_locked_by_other(self, node_id, user_id) -> None:
        """Raise :class:`OfficeDocLockedError` iff a LIVE lease is held by
        someone else — the gate a save must pass (epic D4)."""
        lease = self._lease_repository.find_by_node_id(node_id)
        now = utcnow()
        if (
            lease is not None
            and lease.expires_at > now
            and lease.holder_user_id != user_id
        ):
            raise OfficeDocLockedError(lease.holder_user_id, lease.expires_at)
