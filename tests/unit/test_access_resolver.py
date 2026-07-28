"""``AccessResolver`` truth table — the slice's core test (S147-2 test #1).

Owner / named / link / anonymous, crossed with view / comment / edit and
valid / expired / revoked / wrong-token, run against in-memory fakes (no DB,
per the plugin's unit-test convention — mirrors ``test_document_service.py``).
Every route, authenticated or public, asks this ONE object for its answer;
this test is what proves that single answer is right.
"""
import uuid
from datetime import timedelta

import pytest

from plugins.office.office.models.office_share import (
    PERMISSION_COMMENT,
    PERMISSION_EDIT,
    PERMISSION_VIEW,
)
from plugins.office.office.services.access_resolver import ACCESS_OWNER, AccessResolver
from plugins.office.office.services.share_token import (
    generate_share_token,
    hash_share_token,
)
from vbwd.utils.datetime_utils import utcnow


class FakeNode:
    def __init__(self, owner_user_id, parent_id=None):
        self.id = uuid.uuid4()
        self.owner_user_id = owner_user_id
        self.parent_id = parent_id


class FakeNodeRepository:
    def __init__(self):
        self._nodes = {}

    def add(self, node):
        self._nodes[node.id] = node
        return node

    def find_by_id(self, node_id):
        return self._nodes.get(node_id)


class FakeShare:
    def __init__(
        self,
        node_id,
        permission,
        *,
        subject_user_id=None,
        token_hash=None,
        allow_anonymous=False,
        revoked_at=None,
        expires_at=None,
    ):
        self.id = uuid.uuid4()
        self.node_id = node_id
        self.permission = permission
        self.subject_user_id = subject_user_id
        self.token_hash = token_hash
        self.allow_anonymous = allow_anonymous
        self.revoked_at = revoked_at
        self.expires_at = expires_at


class FakeShareRepository:
    def __init__(self):
        self._by_node = {}

    def add(self, share):
        self._by_node.setdefault(share.node_id, []).append(share)
        return share

    def find_by_node_id(self, node_id):
        return list(self._by_node.get(node_id, []))


@pytest.fixture
def node_repository():
    return FakeNodeRepository()


@pytest.fixture
def share_repository():
    return FakeShareRepository()


@pytest.fixture
def resolver(node_repository, share_repository):
    return AccessResolver(node_repository, share_repository)


def _make_node(node_repository, owner_user_id, parent_id=None):
    return node_repository.add(FakeNode(owner_user_id, parent_id))


def _issue_link_token(share_repository, node_id, permission, **kwargs):
    plaintext = generate_share_token()
    share_repository.add(
        FakeShare(node_id, permission, token_hash=hash_share_token(plaintext), **kwargs)
    )
    return plaintext


# ---------------------------------------------------------------------------
# Owner always wins
# ---------------------------------------------------------------------------


def test_owner_gets_owner_access_regardless_of_shares(
    resolver, node_repository, share_repository
):
    owner_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)

    assert resolver.resolve(node.id, user_id=owner_id) == ACCESS_OWNER


def test_unknown_node_resolves_to_none(resolver):
    assert resolver.resolve(uuid.uuid4(), user_id=uuid.uuid4()) is None


def test_no_matching_share_and_not_owner_resolves_to_none(resolver, node_repository):
    owner_id = uuid.uuid4()
    stranger_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)

    assert resolver.resolve(node.id, user_id=stranger_id) is None


# ---------------------------------------------------------------------------
# Named shares — identified by session, not token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "permission", [PERMISSION_VIEW, PERMISSION_COMMENT, PERMISSION_EDIT]
)
def test_named_share_grants_its_permission_to_the_named_user(
    resolver, node_repository, share_repository, permission
):
    owner_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)
    share_repository.add(FakeShare(node.id, permission, subject_user_id=recipient_id))

    assert resolver.resolve(node.id, user_id=recipient_id) == permission


def test_named_share_does_not_grant_access_to_a_different_user(
    resolver, node_repository, share_repository
):
    owner_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    stranger_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)
    share_repository.add(
        FakeShare(node.id, PERMISSION_VIEW, subject_user_id=recipient_id)
    )

    assert resolver.resolve(node.id, user_id=stranger_id) is None


def test_revoked_named_share_never_matches(resolver, node_repository, share_repository):
    owner_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)
    share_repository.add(
        FakeShare(
            node.id,
            PERMISSION_EDIT,
            subject_user_id=recipient_id,
            revoked_at=utcnow(),
        )
    )

    assert resolver.resolve(node.id, user_id=recipient_id) is None


def test_expired_named_share_never_matches(resolver, node_repository, share_repository):
    owner_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)
    share_repository.add(
        FakeShare(
            node.id,
            PERMISSION_EDIT,
            subject_user_id=recipient_id,
            expires_at=utcnow() - timedelta(seconds=1),
        )
    )

    assert resolver.resolve(node.id, user_id=recipient_id) is None


def test_not_yet_expired_named_share_matches(
    resolver, node_repository, share_repository
):
    owner_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)
    share_repository.add(
        FakeShare(
            node.id,
            PERMISSION_VIEW,
            subject_user_id=recipient_id,
            expires_at=utcnow() + timedelta(hours=1),
        )
    )

    assert resolver.resolve(node.id, user_id=recipient_id) == PERMISSION_VIEW


# ---------------------------------------------------------------------------
# Link shares — the bearer of the token gets the permission
# ---------------------------------------------------------------------------


def test_link_share_not_anonymous_requires_some_logged_in_account(
    resolver, node_repository, share_repository
):
    owner_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)
    token = _issue_link_token(
        share_repository, node.id, PERMISSION_VIEW, allow_anonymous=False
    )

    # Logged out entirely -> no access even with the right token.
    assert resolver.resolve(node.id, user_id=None, share_token=token) is None

    # ANY logged-in account -> access (D5: not identity-scoped).
    some_account_id = uuid.uuid4()
    assert (
        resolver.resolve(node.id, user_id=some_account_id, share_token=token)
        == PERMISSION_VIEW
    )


def test_link_share_allow_anonymous_works_fully_logged_out(
    resolver, node_repository, share_repository
):
    owner_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)
    token = _issue_link_token(
        share_repository, node.id, PERMISSION_EDIT, allow_anonymous=True
    )

    assert resolver.resolve(node.id, user_id=None, share_token=token) == PERMISSION_EDIT


def test_wrong_token_never_matches(resolver, node_repository, share_repository):
    owner_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)
    _issue_link_token(share_repository, node.id, PERMISSION_VIEW, allow_anonymous=True)

    forged_token = generate_share_token()
    assert resolver.resolve(node.id, user_id=None, share_token=forged_token) is None


def test_no_token_presented_never_matches_a_link_share(
    resolver, node_repository, share_repository
):
    owner_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)
    _issue_link_token(share_repository, node.id, PERMISSION_VIEW, allow_anonymous=True)

    assert resolver.resolve(node.id, user_id=None, share_token=None) is None


def test_revoked_link_share_never_matches(resolver, node_repository, share_repository):
    owner_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)
    plaintext = generate_share_token()
    share_repository.add(
        FakeShare(
            node.id,
            PERMISSION_VIEW,
            token_hash=hash_share_token(plaintext),
            allow_anonymous=True,
            revoked_at=utcnow(),
        )
    )

    assert resolver.resolve(node.id, user_id=None, share_token=plaintext) is None


def test_expired_link_share_never_matches(resolver, node_repository, share_repository):
    owner_id = uuid.uuid4()
    node = _make_node(node_repository, owner_id)
    plaintext = generate_share_token()
    share_repository.add(
        FakeShare(
            node.id,
            PERMISSION_VIEW,
            token_hash=hash_share_token(plaintext),
            allow_anonymous=True,
            expires_at=utcnow() - timedelta(seconds=1),
        )
    )

    assert resolver.resolve(node.id, user_id=None, share_token=plaintext) is None


# ---------------------------------------------------------------------------
# Folder inheritance — reaches descendants, never leaks upward
# ---------------------------------------------------------------------------


def test_folder_share_reaches_a_grandchild_document(
    resolver, node_repository, share_repository
):
    owner_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    folder = _make_node(node_repository, owner_id)
    subfolder = _make_node(node_repository, owner_id, parent_id=folder.id)
    document = _make_node(node_repository, owner_id, parent_id=subfolder.id)
    share_repository.add(
        FakeShare(folder.id, PERMISSION_COMMENT, subject_user_id=recipient_id)
    )

    assert resolver.resolve(document.id, user_id=recipient_id) == PERMISSION_COMMENT


def test_descendants_own_share_does_not_leak_upward_to_the_parent(
    resolver, node_repository, share_repository
):
    owner_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    folder = _make_node(node_repository, owner_id)
    document = _make_node(node_repository, owner_id, parent_id=folder.id)
    share_repository.add(
        FakeShare(document.id, PERMISSION_EDIT, subject_user_id=recipient_id)
    )

    # The child's share must not resolve when asking about the PARENT.
    assert resolver.resolve(folder.id, user_id=recipient_id) is None
    # But it still resolves for the child itself.
    assert resolver.resolve(document.id, user_id=recipient_id) == PERMISSION_EDIT


# ---------------------------------------------------------------------------
# Most-permissive-match wins
# ---------------------------------------------------------------------------


def test_most_permissive_matching_share_wins(
    resolver, node_repository, share_repository
):
    owner_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    folder = _make_node(node_repository, owner_id)
    document = _make_node(node_repository, owner_id, parent_id=folder.id)
    # A weaker share on the document itself...
    share_repository.add(
        FakeShare(document.id, PERMISSION_VIEW, subject_user_id=recipient_id)
    )
    # ...and a stronger inherited share on the parent folder.
    share_repository.add(
        FakeShare(folder.id, PERMISSION_EDIT, subject_user_id=recipient_id)
    )

    assert resolver.resolve(document.id, user_id=recipient_id) == PERMISSION_EDIT
