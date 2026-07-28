"""``SharingService`` unit coverage (S147-2) — in-memory fakes, no DB, per the
plugin's unit-test convention. Covers ownership-gated owner-side CRUD, the
public read/write flows through ``AccessResolver``, and controls C3
(password unlock), C4 (anonymous attribution), and C5 (quota charged to the
owner). HTTP-level behaviour (status codes, headers, C1's attachment
disposition) is covered by the integration suite.
"""
import uuid

import pytest

from vbwd.services.filesystem.memory import InMemoryFilesystemManager
from vbwd.utils.datetime_utils import utcnow

from plugins.office.office.models.office_share import PERMISSION_EDIT, PERMISSION_VIEW
from plugins.office.office.services.access_resolver import AccessResolver
from plugins.office.office.services.document_service import OfficeDocumentService
from plugins.office.office.services.exceptions import (
    OfficeNodeNotFoundError,
    OfficeShareForbiddenError,
    OfficeShareNotFoundError,
    OfficeShareRecipientNotFoundError,
    OfficeShareUnlockFailedError,
    OfficeSharePasswordRequiredError,
)
from plugins.office.office.services.mime_sniffer import MimeSniffer
from plugins.office.office.services.quota_service import QuotaService
from plugins.office.office.services.share_grant import ShareGrantService
from plugins.office.office.services.sharing_service import SharingService
from plugins.office.office.storage.document_store import DocumentStore

DEFAULT_MAX_UPLOAD_BYTES = 10_000
DEFAULT_FREE_QUOTA_BYTES = 10_000


class FakeNodeRepository:
    def __init__(self):
        self._nodes = {}

    def add(self, node):
        if node.id is None:
            node.id = uuid.uuid4()
        self._nodes[node.id] = node
        return node

    def find_by_id(self, node_id):
        return self._nodes.get(node_id)

    def find_by_id_for_owner(self, node_id, owner_user_id):
        node = self._nodes.get(node_id)
        if node is None or node.owner_user_id != owner_user_id:
            return None
        return node


class FakeDocumentRepository:
    def __init__(self):
        self._documents_by_node_id = {}

    def add(self, document):
        if document.id is None:
            document.id = uuid.uuid4()
        self._documents_by_node_id[document.node_id] = document
        return document

    def find_by_node_id(self, node_id):
        return self._documents_by_node_id.get(node_id)


class FakeVersionRepository:
    def __init__(self):
        self._versions = []

    def add(self, version):
        if version.id is None:
            version.id = uuid.uuid4()
        self._versions.append(version)
        return version

    def find_by_id(self, version_id):
        for version in self._versions:
            if version.id == version_id:
                return version
        return None

    def find_by_document_id(self, document_id):
        return sorted(
            (v for v in self._versions if v.document_id == document_id),
            key=lambda v: v.version_no,
        )

    def find_by_document_and_version_no(self, document_id, version_no):
        for version in self._versions:
            if version.document_id == document_id and version.version_no == version_no:
                return version
        return None

    def next_version_no(self, document_id):
        existing = self.find_by_document_id(document_id)
        return (existing[-1].version_no + 1) if existing else 1


class FakeUsageRow:
    def __init__(self, bytes_used):
        self.bytes_used = bytes_used


class FakeUsageRepository:
    def __init__(self):
        self._rows_by_user = {}

    def find_by_user_id(self, user_id):
        return self._rows_by_user.get(user_id)

    def increment(self, user_id, delta_bytes):
        current = self._rows_by_user.get(user_id)
        new_total = max(0, (current.bytes_used if current else 0) + delta_bytes)
        self._rows_by_user[user_id] = FakeUsageRow(new_total)
        return self._rows_by_user[user_id]


class FakeUser:
    def __init__(self, email):
        self.id = uuid.uuid4()
        self.email = email


class FakeUserRepository:
    def __init__(self):
        self._users_by_email = {}

    def add(self, user):
        self._users_by_email[user.email] = user
        return user

    def find_by_email(self, email):
        return self._users_by_email.get(email)


@pytest.fixture
def node_repository():
    return FakeNodeRepository()


@pytest.fixture
def document_repository():
    return FakeDocumentRepository()


class FakeShareRepository:
    """In-memory stand-in with the SAME contract as
    ``OfficeShareRepository`` (Liskov) — no DB session needed for unit tests.
    """

    def __init__(self):
        self._shares = {}

    def add(self, share):
        if share.id is None:
            share.id = uuid.uuid4()
        self._shares[share.id] = share
        return share

    def find_by_id(self, share_id):
        return self._shares.get(share_id)

    def find_by_id_for_owner(self, share_id, owner_user_id, *, node_repository=None):
        share = self._shares.get(share_id)
        return share

    def find_by_node_id(self, node_id):
        return [s for s in self._shares.values() if s.node_id == node_id]

    def find_by_token_hash(self, token_hash):
        for share in self._shares.values():
            if share.token_hash == token_hash:
                return share
        return None

    def find_active_named_shares_for_user(self, user_id):
        now = utcnow()
        return [
            s
            for s in self._shares.values()
            if s.subject_user_id == user_id
            and s.revoked_at is None
            and (s.expires_at is None or s.expires_at > now)
        ]


class FakeShareAccessRepository:
    def __init__(self):
        self.entries = []

    def add(self, access):
        self.entries.append(access)
        return access

    def find_by_share_id(self, share_id):
        return [entry for entry in self.entries if entry.share_id == share_id]


class OwnerCheckingFakeShareRepository(FakeShareRepository):
    """Adds the real owner-join semantics ``SharingService`` relies on."""

    def __init__(self, node_repository):
        super().__init__()
        self._node_repository = node_repository

    def find_by_id_for_owner(self, share_id, owner_user_id):
        share = self._shares.get(share_id)
        if share is None:
            return None
        node = self._node_repository.find_by_id(share.node_id)
        if node is None or node.owner_user_id != owner_user_id:
            return None
        return share


@pytest.fixture
def document_service(node_repository, document_repository):
    filespace = InMemoryFilesystemManager().for_plugin("office")
    return OfficeDocumentService(
        node_repository=node_repository,
        document_repository=document_repository,
        version_repository=FakeVersionRepository(),
        quota_service=QuotaService(FakeUsageRepository(), DEFAULT_FREE_QUOTA_BYTES),
        document_store=DocumentStore(filespace),
        mime_sniffer=MimeSniffer(),
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
    )


@pytest.fixture
def user_repository():
    return FakeUserRepository()


@pytest.fixture
def sharing_service(
    node_repository, document_repository, document_service, user_repository
):
    share_repository = OwnerCheckingFakeShareRepository(node_repository)
    access_resolver = AccessResolver(node_repository, share_repository)
    return SharingService(
        node_repository=node_repository,
        document_repository=document_repository,
        share_repository=share_repository,
        share_access_repository=FakeShareAccessRepository(),
        document_service=document_service,
        access_resolver=access_resolver,
        user_repository=user_repository,
        grant_service=ShareGrantService(),
    )


@pytest.fixture
def owner_id():
    return uuid.uuid4()


@pytest.fixture
def uploaded_document(document_service, owner_id):
    node, document, _version = document_service.upload_document(
        owner_id, "notes.txt", None, b"secret plans"
    )
    return node, document


# ---------------------------------------------------------------------------
# Owner-side CRUD
# ---------------------------------------------------------------------------


def test_create_share_returns_the_plaintext_token_exactly_once(
    sharing_service, owner_id, uploaded_document
):
    node, _document = uploaded_document

    share, token = sharing_service.create_share(
        owner_id, node.id, permission=PERMISSION_VIEW
    )

    assert token  # a real plaintext value
    assert share.token_hash != token  # never stored raw
    assert "token" not in share.to_dict()


def test_create_share_on_a_node_you_do_not_own_is_not_found(
    sharing_service, uploaded_document
):
    node, _document = uploaded_document
    stranger_id = uuid.uuid4()

    with pytest.raises(OfficeNodeNotFoundError):
        sharing_service.create_share(stranger_id, node.id, permission=PERMISSION_VIEW)


def test_create_named_share_resolves_recipient_by_email(
    sharing_service, owner_id, uploaded_document, user_repository
):
    node, _document = uploaded_document
    recipient = user_repository.add(FakeUser("friend@example.com"))

    share, _token = sharing_service.create_share(
        owner_id,
        node.id,
        permission=PERMISSION_EDIT,
        recipient_email="friend@example.com",
    )

    assert share.subject_user_id == recipient.id


def test_create_named_share_with_unknown_email_raises(
    sharing_service, owner_id, uploaded_document
):
    node, _document = uploaded_document

    with pytest.raises(OfficeShareRecipientNotFoundError):
        sharing_service.create_share(
            owner_id,
            node.id,
            permission=PERMISSION_VIEW,
            recipient_email="ghost@example.com",
        )


def test_revoke_share_sets_revoked_at_and_public_access_stops(
    sharing_service, owner_id, uploaded_document
):
    node, _document = uploaded_document
    _share, token = sharing_service.create_share(
        owner_id, node.id, permission=PERMISSION_VIEW, allow_anonymous=True
    )

    sharing_service.get_public_metadata(token)  # works before revoke

    sharing_service.revoke_share(owner_id, _share.id)

    with pytest.raises(OfficeShareNotFoundError):
        sharing_service.get_public_metadata(token)


def test_update_share_changes_permission(sharing_service, owner_id, uploaded_document):
    node, _document = uploaded_document
    share, _token = sharing_service.create_share(
        owner_id, node.id, permission=PERMISSION_VIEW
    )

    updated = sharing_service.update_share(
        owner_id, share.id, permission=PERMISSION_EDIT
    )

    assert updated.permission == PERMISSION_EDIT


def test_list_shared_with_me_returns_named_shares(
    sharing_service, owner_id, uploaded_document, user_repository
):
    node, _document = uploaded_document
    recipient = user_repository.add(FakeUser("friend@example.com"))
    sharing_service.create_share(
        owner_id,
        node.id,
        permission=PERMISSION_VIEW,
        recipient_email="friend@example.com",
    )

    results = sharing_service.list_shared_with_me(recipient.id)

    assert len(results) == 1
    shared_share, shared_node = results[0]
    assert shared_node.id == node.id
    assert shared_share.subject_user_id == recipient.id


def test_named_recipient_can_read_shared_document_via_their_own_session(
    sharing_service, owner_id, uploaded_document, user_repository
):
    node, _document = uploaded_document
    recipient = user_repository.add(FakeUser("friend@example.com"))
    sharing_service.create_share(
        owner_id,
        node.id,
        permission=PERMISSION_VIEW,
        recipient_email="friend@example.com",
    )

    _node, _doc, _version, content = sharing_service.get_shared_document_content(
        recipient.id, node.id
    )

    assert content == b"secret plans"


def test_unrelated_user_cannot_read_via_shared_with_me(
    sharing_service, owner_id, uploaded_document
):
    node, _document = uploaded_document
    stranger_id = uuid.uuid4()

    with pytest.raises(OfficeNodeNotFoundError):
        sharing_service.get_shared_document_content(stranger_id, node.id)


def test_owner_can_also_read_via_shared_document_reader(
    sharing_service, owner_id, uploaded_document
):
    node, _document = uploaded_document

    metadata = sharing_service.get_shared_document_metadata(owner_id, node.id)

    assert metadata["id"] == str(node.id)


# ---------------------------------------------------------------------------
# Public read/write
# ---------------------------------------------------------------------------


def test_anonymous_disallowed_link_is_not_found_when_logged_out(
    sharing_service, owner_id, uploaded_document
):
    node, _document = uploaded_document
    _share, token = sharing_service.create_share(
        owner_id, node.id, permission=PERMISSION_VIEW, allow_anonymous=False
    )

    with pytest.raises(OfficeShareNotFoundError):
        sharing_service.get_public_metadata(token, user_id=None)


def test_random_token_is_not_found(sharing_service):
    with pytest.raises(OfficeShareNotFoundError):
        sharing_service.get_public_metadata("not-a-real-token")


def test_get_public_content_round_trips_bytes(
    sharing_service, owner_id, uploaded_document
):
    node, _document = uploaded_document
    _share, token = sharing_service.create_share(
        owner_id, node.id, permission=PERMISSION_VIEW, allow_anonymous=True
    )

    _node, _doc, _version, content = sharing_service.get_public_content(token)

    assert content == b"secret plans"


def test_view_share_cannot_write_content(sharing_service, owner_id, uploaded_document):
    node, _document = uploaded_document
    _share, token = sharing_service.create_share(
        owner_id, node.id, permission=PERMISSION_VIEW, allow_anonymous=True
    )

    with pytest.raises(OfficeShareForbiddenError):
        sharing_service.put_public_content(token, b"new content")


def test_anonymous_edit_is_attributed_to_the_share_not_a_user(
    sharing_service, owner_id, uploaded_document
):
    node, _document = uploaded_document
    share, token = sharing_service.create_share(
        owner_id, node.id, permission=PERMISSION_EDIT, allow_anonymous=True
    )

    version = sharing_service.put_public_content(token, b"edited anonymously")

    assert version.created_by_user_id is None
    assert version.created_by_share_id == share.id


def test_anonymous_edit_charges_the_owners_quota(
    sharing_service, owner_id, uploaded_document, document_service
):
    node, _document = uploaded_document
    _share, token = sharing_service.create_share(
        owner_id, node.id, permission=PERMISSION_EDIT, allow_anonymous=True
    )
    before = document_service._quota_service.bytes_used(owner_id)  # noqa: SLF001

    sharing_service.put_public_content(token, b"more bytes than before!!")

    after = document_service._quota_service.bytes_used(owner_id)  # noqa: SLF001
    assert after > before


def test_logged_in_stranger_edit_via_link_is_attributed_to_them(
    sharing_service, owner_id, uploaded_document
):
    node, _document = uploaded_document
    _share, token = sharing_service.create_share(
        owner_id, node.id, permission=PERMISSION_EDIT, allow_anonymous=False
    )
    stranger_id = uuid.uuid4()

    version = sharing_service.put_public_content(token, b"edited", user_id=stranger_id)

    assert version.created_by_user_id == stranger_id
    assert version.created_by_share_id is None


# ---------------------------------------------------------------------------
# Password unlock (C3)
# ---------------------------------------------------------------------------


def test_password_protected_content_requires_unlock_first(
    sharing_service, owner_id, uploaded_document
):
    node, _document = uploaded_document
    _share, token = sharing_service.create_share(
        owner_id,
        node.id,
        permission=PERMISSION_VIEW,
        allow_anonymous=True,
        password="hunter2",
    )

    with pytest.raises(OfficeSharePasswordRequiredError):
        sharing_service.get_public_content(token)


def test_wrong_password_fails_to_unlock(sharing_service, owner_id, uploaded_document):
    node, _document = uploaded_document
    _share, token = sharing_service.create_share(
        owner_id,
        node.id,
        permission=PERMISSION_VIEW,
        allow_anonymous=True,
        password="hunter2",
    )

    with pytest.raises(OfficeShareUnlockFailedError):
        sharing_service.unlock_share(token, "wrong-password")


def test_correct_password_unlocks_and_grant_permits_content(
    sharing_service, owner_id, uploaded_document
):
    node, _document = uploaded_document
    _share, token = sharing_service.create_share(
        owner_id,
        node.id,
        permission=PERMISSION_VIEW,
        allow_anonymous=True,
        password="hunter2",
    )

    grant = sharing_service.unlock_share(token, "hunter2")
    _node, _doc, _version, content = sharing_service.get_public_content(
        token, grant_token=grant
    )

    assert content == b"secret plans"


def test_grant_for_one_share_does_not_unlock_another(
    sharing_service, owner_id, document_service
):
    node_a, _doc_a, _v = document_service.upload_document(
        owner_id, "a.txt", None, b"aaa"
    )
    node_b, _doc_b, _v = document_service.upload_document(
        owner_id, "b.txt", None, b"bbb"
    )
    _share_a, token_a = sharing_service.create_share(
        owner_id,
        node_a.id,
        permission=PERMISSION_VIEW,
        allow_anonymous=True,
        password="secret-a",
    )
    _share_b, token_b = sharing_service.create_share(
        owner_id,
        node_b.id,
        permission=PERMISSION_VIEW,
        allow_anonymous=True,
        password="secret-b",
    )

    grant_for_a = sharing_service.unlock_share(token_a, "secret-a")

    with pytest.raises(OfficeSharePasswordRequiredError):
        sharing_service.get_public_content(token_b, grant_token=grant_for_a)
