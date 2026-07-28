"""S147-3 unit coverage for ``EditLeaseService`` — test #3 (acquire -> a
second user is read-only -> lapse -> take-over succeeds), against an
in-memory fake repository (no DB)."""
import uuid
from datetime import datetime, timedelta

import pytest

from plugins.office.office.services.edit_lease_service import EditLeaseService
from plugins.office.office.services.exceptions import OfficeDocLockedError


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class FakeEditLeaseRepository:
    def __init__(self) -> None:
        self._leases = {}

    def find_by_node_id(self, node_id):
        return self._leases.get(node_id)

    def upsert(self, node_id, holder_user_id, acquired_at, expires_at):
        lease = self._leases.setdefault(node_id, _FakeLeaseRow(node_id))
        lease.holder_user_id = holder_user_id
        lease.acquired_at = acquired_at
        lease.expires_at = expires_at
        return lease

    def delete(self, node_id):
        self._leases.pop(node_id, None)


class _FakeLeaseRow:
    def __init__(self, node_id):
        self.node_id = node_id
        self.holder_user_id = None
        self.acquired_at = None
        self.expires_at = None


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 1, 1, 12, 0, 0))


@pytest.fixture
def service(clock, monkeypatch):
    repository = FakeEditLeaseRepository()
    lease_service = EditLeaseService(repository, lease_seconds=90)
    monkeypatch.setattr(
        "plugins.office.office.services.edit_lease_service.utcnow", lambda: clock.now
    )
    return lease_service


def test_first_acquire_is_granted_and_marked_self(service):
    node_id, user_id = uuid.uuid4(), uuid.uuid4()
    state = service.acquire(node_id, user_id)
    assert state.granted is True
    assert state.is_self is True
    assert state.holder_user_id == user_id


def test_second_user_acquire_while_live_is_read_only(service):
    node_id, first_user, second_user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    service.acquire(node_id, first_user)

    state = service.acquire(node_id, second_user)

    assert state.granted is False
    assert state.is_self is False
    assert state.holder_user_id == first_user


def test_same_holder_reacquire_renews_the_lease(service, clock):
    node_id, user_id = uuid.uuid4(), uuid.uuid4()
    first = service.acquire(node_id, user_id)
    clock.advance(10)
    second = service.acquire(node_id, user_id)
    assert second.expires_at > first.expires_at


def test_takeover_succeeds_once_the_lease_has_lapsed(service, clock):
    node_id, first_user, second_user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    service.acquire(node_id, first_user)

    clock.advance(91)  # past the 90s lease

    state = service.acquire(node_id, second_user)
    assert state.granted is True
    assert state.holder_user_id == second_user


def test_release_by_a_non_holder_is_a_no_op(service):
    node_id, holder, stranger = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    service.acquire(node_id, holder)

    service.release(node_id, stranger)

    current = service.current(node_id, holder)
    assert current.held is True
    assert current.holder_user_id == holder


def test_release_by_the_holder_frees_the_lease(service):
    node_id, holder = uuid.uuid4(), uuid.uuid4()
    service.acquire(node_id, holder)

    service.release(node_id, holder)

    current = service.current(node_id, holder)
    assert current.held is False


def test_current_reports_unheld_when_no_lease_exists(service):
    node_id, user_id = uuid.uuid4(), uuid.uuid4()
    state = service.current(node_id, user_id)
    assert state.held is False
    assert state.granted is True


def test_assert_not_locked_by_other_raises_while_a_stranger_holds_it(service):
    node_id, holder, stranger = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    service.acquire(node_id, holder)

    with pytest.raises(OfficeDocLockedError):
        service.assert_not_locked_by_other(node_id, stranger)


def test_assert_not_locked_by_other_passes_for_the_holder(service):
    node_id, holder = uuid.uuid4(), uuid.uuid4()
    service.acquire(node_id, holder)

    service.assert_not_locked_by_other(node_id, holder)  # does not raise


def test_assert_not_locked_by_other_passes_once_lapsed(service, clock):
    node_id, holder, stranger = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    service.acquire(node_id, holder)
    clock.advance(91)

    service.assert_not_locked_by_other(node_id, stranger)  # does not raise
