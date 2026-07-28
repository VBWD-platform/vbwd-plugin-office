"""S147-1 test #3/#4 — quota enforcement (B2).

``QuotaService`` is exercised against a fake usage repository (no DB) and the
D3-default entitlement provider (allow, with no plan-driven override) so the
free-tier config value is what gates capacity.
"""
import uuid

import pytest

from plugins.office.office.services.quota_service import (
    OfficeQuotaExceededError,
    QuotaService,
)


class FakeUsageRow:
    """Mirrors the one attribute ``QuotaService`` reads off ``OfficeUsage``."""

    def __init__(self, bytes_used: int) -> None:
        self.bytes_used = bytes_used


class FakeUsageRepository:
    """Liskov-honouring in-memory double for ``OfficeUsageRepository``."""

    def __init__(self):
        self._rows_by_user = {}

    def find_by_user_id(self, user_id):
        return self._rows_by_user.get(user_id)

    def increment(self, user_id, delta_bytes):
        current = self._rows_by_user.get(user_id)
        new_total = max(0, (current.bytes_used if current else 0) + delta_bytes)
        self._rows_by_user[user_id] = FakeUsageRow(new_total)
        return self._rows_by_user[user_id]


@pytest.fixture
def user_id():
    return uuid.uuid4()


def test_bytes_used_is_zero_for_a_fresh_user(user_id):
    service = QuotaService(FakeUsageRepository(), free_quota_bytes=1000)

    assert service.bytes_used(user_id) == 0


def test_quota_bytes_defaults_to_the_free_tier_config(user_id):
    service = QuotaService(FakeUsageRepository(), free_quota_bytes=1234)

    assert service.quota_bytes(user_id) == 1234


def test_ensure_capacity_allows_a_write_within_quota(user_id):
    service = QuotaService(FakeUsageRepository(), free_quota_bytes=1000)

    service.ensure_capacity(user_id, 999)  # must not raise


def test_ensure_capacity_allows_filling_exactly_to_the_quota(user_id):
    service = QuotaService(FakeUsageRepository(), free_quota_bytes=1000)

    service.ensure_capacity(user_id, 1000)  # exactly at limit — must not raise


def test_ensure_capacity_rejects_a_write_that_crosses_the_quota(user_id):
    repository = FakeUsageRepository()
    service = QuotaService(repository, free_quota_bytes=1000)
    repository.increment(user_id, 1000)  # already AT the limit

    with pytest.raises(OfficeQuotaExceededError):
        service.ensure_capacity(user_id, 1)


def test_apply_delta_updates_bytes_used(user_id):
    service = QuotaService(FakeUsageRepository(), free_quota_bytes=1000)

    service.apply_delta(user_id, 400)
    service.apply_delta(user_id, 100)

    assert service.bytes_used(user_id) == 500


def test_apply_delta_can_free_bytes_on_purge(user_id):
    service = QuotaService(FakeUsageRepository(), free_quota_bytes=1000)
    service.apply_delta(user_id, 400)

    service.apply_delta(user_id, -400)

    assert service.bytes_used(user_id) == 0
