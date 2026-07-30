"""S147-1 unit coverage for ``OfficeDocumentService`` — tests #3/#4 (quota),
#7 (restore appends), #8 (trash/purge), #9 (owner-scoped 404) — against
in-memory fakes (no DB, per the plugin unit-test convention)."""
import uuid

import pytest

from vbwd.services.filesystem.memory import InMemoryFilesystemManager

from plugins.office.office.models.office_node import NODE_KIND_DOCUMENT
from plugins.office.office.services.document_service import OfficeDocumentService
from plugins.office.office.services.exceptions import (
    OfficeInvalidNodeError,
    OfficeNodeNotFoundError,
    OfficeUploadTooLargeError,
)
from plugins.office.office.services.mime_sniffer import MimeSniffer
from plugins.office.office.services.quota_service import (
    OfficeQuotaExceededError,
    QuotaService,
)
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

    def delete(self, node):
        self._nodes.pop(node.id, None)

    def find_by_id(self, node_id):
        return self._nodes.get(node_id)

    def find_by_id_for_owner(self, node_id, owner_user_id):
        node = self._nodes.get(node_id)
        if node is None or node.owner_user_id != owner_user_id:
            return None
        return node

    def find_children(self, owner_user_id, parent_id, include_trashed=False):
        results = [
            node
            for node in self._nodes.values()
            if node.owner_user_id == owner_user_id and node.parent_id == parent_id
        ]
        if not include_trashed:
            results = [node for node in results if node.trashed_at is None]
        return sorted(results, key=lambda node: node.name)


class FakeDocumentRepository:
    def __init__(self):
        self._documents_by_node_id = {}

    def add(self, document):
        if document.id is None:
            document.id = uuid.uuid4()
        self._documents_by_node_id[document.node_id] = document
        return document

    def find_by_id(self, document_id):
        for document in self._documents_by_node_id.values():
            if document.id == document_id:
                return document
        return None

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


@pytest.fixture
def service_factory():
    def _build(
        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
        free_quota_bytes=DEFAULT_FREE_QUOTA_BYTES,
    ):
        filespace = InMemoryFilesystemManager().for_plugin("office")
        return OfficeDocumentService(
            node_repository=FakeNodeRepository(),
            document_repository=FakeDocumentRepository(),
            version_repository=FakeVersionRepository(),
            quota_service=QuotaService(FakeUsageRepository(), free_quota_bytes),
            document_store=DocumentStore(filespace),
            mime_sniffer=MimeSniffer(),
            max_upload_bytes=max_upload_bytes,
        )

    return _build


@pytest.fixture
def owner_id():
    return uuid.uuid4()


def test_upload_document_creates_a_node_document_and_first_version(
    service_factory, owner_id
):
    service = service_factory()

    node, document, version = service.upload_document(
        owner_id, "report.txt", None, b"hello"
    )

    assert node.kind == NODE_KIND_DOCUMENT
    assert document.node_id == node.id
    assert version.version_no == 1
    assert version.size_bytes == 5


def test_upload_over_the_cap_raises_before_any_write(service_factory, owner_id):
    service = service_factory(max_upload_bytes=4)

    with pytest.raises(OfficeUploadTooLargeError):
        service.upload_document(owner_id, "big.bin", None, b"12345")


def test_upload_at_quota_limit_is_rejected(service_factory, owner_id):
    service = service_factory(free_quota_bytes=5)
    service.upload_document(owner_id, "first.txt", None, b"12345")  # fills the quota

    with pytest.raises(OfficeQuotaExceededError):
        service.upload_document(owner_id, "second.txt", None, b"1")


def test_usage_updates_on_upload_and_new_version(service_factory, owner_id):
    service = service_factory()
    node, _, _ = service.upload_document(owner_id, "a.txt", None, b"12345")

    service.add_version(owner_id, node.id, b"1234567890")

    assert service._quota_service.bytes_used(owner_id) == 15


def test_add_version_over_quota_is_rejected_and_does_not_grow_usage(
    service_factory, owner_id
):
    service = service_factory(free_quota_bytes=6)
    node, _, _ = service.upload_document(owner_id, "a.txt", None, b"12345")  # 5 bytes

    with pytest.raises(OfficeQuotaExceededError):
        service.add_version(owner_id, node.id, b"12")  # would bring total to 7 > 6

    assert service._quota_service.bytes_used(owner_id) == 5


def test_restore_version_appends_rather_than_mutating(service_factory, owner_id):
    service = service_factory()
    node, document, v1 = service.upload_document(owner_id, "a.txt", None, b"first")
    service.add_version(owner_id, node.id, b"second-version")

    restored = service.restore_version(owner_id, node.id, 1)

    assert restored.version_no == 3  # appended, not overwritten
    assert restored.storage_key == v1.storage_key  # pointer swap
    versions = service.list_versions(owner_id, node.id)
    assert [v.version_no for v in versions] == [1, 2, 3]  # full history kept


def test_restore_version_does_not_consume_extra_quota(service_factory, owner_id):
    service = service_factory()
    node, _, _ = service.upload_document(owner_id, "a.txt", None, b"first")
    before = service._quota_service.bytes_used(owner_id)

    service.restore_version(owner_id, node.id, 1)

    assert service._quota_service.bytes_used(owner_id) == before


def test_trash_hides_from_listing_but_preserves_the_content(service_factory, owner_id):
    service = service_factory()
    node, document, version = service.upload_document(owner_id, "a.txt", None, b"kept")

    service.trash_node(owner_id, node.id)

    assert [s.node.id for s in service.list_children(owner_id, None)] == []
    _, _, _, content = service.get_content(owner_id, node.id)
    assert content == b"kept"


def test_purge_frees_quota_and_removes_the_blob(service_factory, owner_id):
    service = service_factory()
    node, _, _ = service.upload_document(owner_id, "a.txt", None, b"12345")
    assert service._quota_service.bytes_used(owner_id) == 5

    service.purge_node(owner_id, node.id)

    assert service._quota_service.bytes_used(owner_id) == 0
    with pytest.raises(OfficeNodeNotFoundError):
        service.get_document_metadata(owner_id, node.id)


def test_purge_after_restore_frees_bytes_only_once_per_distinct_blob(
    service_factory, owner_id
):
    service = service_factory()
    node, _, _ = service.upload_document(owner_id, "a.txt", None, b"12345")  # 5 bytes
    service.restore_version(owner_id, node.id, 1)  # reuses the same blob, +0 bytes
    assert service._quota_service.bytes_used(owner_id) == 5

    service.purge_node(owner_id, node.id)

    assert service._quota_service.bytes_used(owner_id) == 0


def test_another_owners_node_id_is_not_found_never_forbidden(service_factory, owner_id):
    service = service_factory()
    node, _, _ = service.upload_document(owner_id, "a.txt", None, b"secret")
    someone_else = uuid.uuid4()

    with pytest.raises(OfficeNodeNotFoundError):
        service.get_document_metadata(someone_else, node.id)


def test_move_folder_into_its_own_descendant_is_rejected(service_factory, owner_id):
    service = service_factory()
    parent = service.create_folder(owner_id, "parent")
    child = service.create_folder(owner_id, "child", parent.id)

    with pytest.raises(OfficeInvalidNodeError):
        service.rename_or_move(owner_id, parent.id, parent_id=child.id)


def test_rename_only_touches_the_name(service_factory, owner_id):
    service = service_factory()
    folder = service.create_folder(owner_id, "old-name")

    service.rename_or_move(owner_id, folder.id, name="new-name")

    assert folder.name == "new-name"
    assert folder.parent_id is None


def test_rename_with_path_traversal_name_does_not_change_storage_key(
    service_factory, owner_id
):
    """The security ask: renaming can never relocate a document's bytes.
    Storage keys are server-minted (owner/document/version); a hostile
    display name is sanitised but the blob stays exactly where it was."""
    service = service_factory()
    node, _document, version = service.upload_document(
        owner_id, "report.txt", None, b"unchanged bytes"
    )
    original_storage_key = version.storage_key

    service.rename_or_move(owner_id, node.id, name="../../etc/passwd")

    assert "/" not in node.name
    assert "\\" not in node.name
    _, _, current_version, content = service.get_content(owner_id, node.id)
    assert current_version.storage_key == original_storage_key
    assert content == b"unchanged bytes"


# ---------------------------------------------------------------------------
# Copy (Finder-style file management)
# ---------------------------------------------------------------------------


def test_copy_document_creates_a_new_node_document_and_version(
    service_factory, owner_id
):
    service = service_factory()
    source_node, source_document, _ = service.upload_document(
        owner_id, "report.txt", None, b"original content"
    )

    copy_node = service.copy_node(owner_id, source_node.id)

    assert copy_node.id != source_node.id
    assert copy_node.name == "report copy.txt"
    _, copy_document = service._require_document(owner_id, copy_node.id)
    assert copy_document.id != source_document.id
    _, _, _, content = service.get_content(owner_id, copy_node.id)
    assert content == b"original content"


def test_copy_document_charges_quota_for_the_new_copy(service_factory, owner_id):
    service = service_factory()
    source_node, _, _ = service.upload_document(owner_id, "a.txt", None, b"12345")
    used_before_copy = service._quota_service.bytes_used(owner_id)

    service.copy_node(owner_id, source_node.id)

    assert service._quota_service.bytes_used(owner_id) == used_before_copy + 5


def test_copy_document_name_collision_gets_a_copy_suffix(service_factory, owner_id):
    service = service_factory()
    source_node, _, _ = service.upload_document(owner_id, "report.txt", None, b"a")

    first_copy = service.copy_node(owner_id, source_node.id)
    second_copy = service.copy_node(owner_id, source_node.id)

    assert first_copy.name == "report copy.txt"
    assert second_copy.name == "report copy 2.txt"


def test_copy_folder_without_extension_gets_a_copy_suffix(service_factory, owner_id):
    service = service_factory()
    folder = service.create_folder(owner_id, "Photos")

    copy_folder = service.copy_node(owner_id, folder.id)

    assert copy_folder.name == "Photos copy"


def test_copy_folder_is_recursive(service_factory, owner_id):
    service = service_factory()
    folder = service.create_folder(owner_id, "parent")
    child_folder = service.create_folder(owner_id, "child", folder.id)
    service.upload_document(owner_id, "a.txt", folder.id, b"top-level")
    service.upload_document(owner_id, "b.txt", child_folder.id, b"nested")

    copy_folder = service.copy_node(owner_id, folder.id)

    top_level_copy = service.list_children(owner_id, copy_folder.id)
    assert {summary.node.name for summary in top_level_copy} == {"a.txt", "child"}
    nested_folder = next(
        summary.node for summary in top_level_copy if summary.node.name == "child"
    )
    nested_children = service.list_children(owner_id, nested_folder.id)
    assert [summary.node.name for summary in nested_children] == ["b.txt"]
    _, _, _, nested_content = service.get_content(owner_id, nested_children[0].node.id)
    assert nested_content == b"nested"


def test_copy_folder_into_its_own_descendant_is_rejected(service_factory, owner_id):
    service = service_factory()
    parent = service.create_folder(owner_id, "parent")
    child = service.create_folder(owner_id, "child", parent.id)

    with pytest.raises(OfficeInvalidNodeError):
        service.copy_node(owner_id, parent.id, parent_id=child.id)


def test_copy_folder_into_itself_is_rejected(service_factory, owner_id):
    service = service_factory()
    folder = service.create_folder(owner_id, "folder")

    with pytest.raises(OfficeInvalidNodeError):
        service.copy_node(owner_id, folder.id, parent_id=folder.id)


def test_copy_another_owners_node_id_is_not_found(service_factory, owner_id):
    service = service_factory()
    node, _, _ = service.upload_document(owner_id, "secret.txt", None, b"top secret")
    someone_else = uuid.uuid4()

    with pytest.raises(OfficeNodeNotFoundError):
        service.copy_node(someone_else, node.id)


def test_copy_folder_subtree_exceeding_quota_writes_nothing(service_factory, owner_id):
    """A folder copy is refused as a whole when the subtree would not fit —
    never a partially-written copy."""
    service = service_factory(free_quota_bytes=12)
    folder = service.create_folder(owner_id, "parent")
    service.upload_document(owner_id, "a.txt", folder.id, b"12345")  # 5 bytes
    service.upload_document(owner_id, "b.txt", folder.id, b"1234567")  # 7 bytes
    used_before_copy = service._quota_service.bytes_used(owner_id)  # 12, at the cap

    with pytest.raises(OfficeQuotaExceededError):
        service.copy_node(owner_id, folder.id)

    assert service._quota_service.bytes_used(owner_id) == used_before_copy
    siblings = service.list_children(owner_id, None)
    assert [summary.node.name for summary in siblings] == ["parent"]
