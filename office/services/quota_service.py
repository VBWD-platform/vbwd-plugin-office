"""``QuotaService`` — per-user storage accounting + enforcement (S147-1 B2).

Usage is materialised on ``office_usage`` (O(1) reads) rather than summed from
``office_version`` on every check. Quota comes from the core entitlement seam
(epic D8): a paid plan can raise ``office.storage_quota_bytes`` through
``IEntitlementProvider.get_feature_value``; the plugin's own
``free_quota_bytes`` config is the default returned when no plan (or no
``subscription`` plugin at all — the D3 default in
``vbwd/services/entitlement.py``) supplies one. This is resolved defensively
so the bundle still works with ``subscription`` disabled.
"""
from __future__ import annotations

from uuid import UUID

from vbwd.services.entitlement import resolve_entitlement_provider

QUOTA_FEATURE_KEY = "office.storage_quota_bytes"


class OfficeQuotaExceededError(Exception):
    """Raised when a write would push a user's usage past their quota."""

    def __init__(
        self,
        user_id: UUID,
        bytes_used: int,
        additional_bytes: int,
        quota_bytes: int,
    ) -> None:
        self.user_id = user_id
        self.bytes_used = bytes_used
        self.additional_bytes = additional_bytes
        self.quota_bytes = quota_bytes
        super().__init__(
            f"Storage quota exceeded for user {user_id}: "
            f"{bytes_used} + {additional_bytes} > {quota_bytes}"
        )


class QuotaService:
    """One reason to change: how a user's storage capacity is computed and
    enforced. Reads/writes route through ``usage_repository`` — the single
    home for the materialised ``office_usage`` row (DRY)."""

    def __init__(self, usage_repository, free_quota_bytes: int) -> None:
        self._usage_repository = usage_repository
        self._free_quota_bytes = free_quota_bytes

    def bytes_used(self, user_id: UUID) -> int:
        usage = self._usage_repository.find_by_user_id(user_id)
        return usage.bytes_used if usage is not None else 0

    def quota_bytes(self, user_id: UUID) -> int:
        provider = resolve_entitlement_provider()
        value = provider.get_feature_value(
            user_id, QUOTA_FEATURE_KEY, default=self._free_quota_bytes
        )
        return int(value) if value is not None else self._free_quota_bytes

    def ensure_capacity(self, user_id: UUID, additional_bytes: int) -> None:
        """Raise :class:`OfficeQuotaExceededError` if writing
        ``additional_bytes`` more would push the user's usage past their
        quota. Filling exactly to the quota is allowed (only crossing it is
        rejected)."""
        used = self.bytes_used(user_id)
        quota = self.quota_bytes(user_id)
        if used + additional_bytes > quota:
            raise OfficeQuotaExceededError(user_id, used, additional_bytes, quota)

    def apply_delta(self, user_id: UUID, delta_bytes: int) -> None:
        """Adjust the materialised usage total. A negative delta frees bytes
        (purge / trash-purge); a positive delta accounts a new blob
        (upload / new version)."""
        self._usage_repository.increment(user_id, delta_bytes)

    def raise_if_over_quota(self, user_id: UUID) -> None:
        """Post-write check (B2): raise if the COMMITTED total now exceeds
        quota, e.g. a concurrent write raced the pre-check in
        :meth:`ensure_capacity`."""
        used = self.bytes_used(user_id)
        quota = self.quota_bytes(user_id)
        if used > quota:
            raise OfficeQuotaExceededError(user_id, used, 0, quota)
