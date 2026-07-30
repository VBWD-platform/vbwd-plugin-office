"""S147-3 unit coverage for ``OfficeDocEditorService`` — the Doc-specific
logic it adds ON TOP of the reused ``OfficeDocumentService``/
``AccessResolver`` (S147-1/-2): stale-version rejection (test #2), the
view-only-cannot-save/lease rule (test #4), and the ``ai_enabled`` gate
(test #8). Against fakes/stubs — no DB, no network.
"""
import uuid

import pytest

from plugins.office.office.services.access_resolver import ACCESS_OWNER
from plugins.office.office.services.doc_content import EMPTY_CONTENT_MODEL
from plugins.office.office.services.doc_editor_service import OfficeDocEditorService
from plugins.office.office.services.edit_lease_service import EditLeaseService
from plugins.office.office.services.exceptions import (
    OfficeAiDisabledError,
    OfficeDocStaleVersionError,
    OfficeNodeNotFoundError,
    OfficeShareForbiddenError,
)


class _Node:
    def __init__(self, node_id, kind="document"):
        self.id = node_id
        self.kind = kind


class _Document:
    def __init__(self, doc_type="text", current_version_id=None, ai_enabled=False):
        self.doc_type = doc_type
        self.current_version_id = current_version_id
        self.ai_enabled = ai_enabled


class _Version:
    def __init__(self, version_id, version_no):
        self.id = version_id
        self.version_no = version_no


class FakeNodeRepository:
    def __init__(self, nodes):
        self._nodes = nodes

    def find_by_id(self, node_id):
        return self._nodes.get(node_id)


class FakeDocumentRepository:
    def __init__(self, documents_by_node_id):
        self._documents = documents_by_node_id

    def find_by_node_id(self, node_id):
        return self._documents.get(node_id)

    def add(self, document):
        return document


class FakeVersionRepository:
    def __init__(self, versions_by_id):
        self._versions = versions_by_id

    def find_by_id(self, version_id):
        return self._versions.get(version_id)


class FakeAccessResolver:
    def __init__(self, access_by_user):
        self._access_by_user = access_by_user

    def resolve(self, node_id, *, user_id=None, share_token=None):
        return self._access_by_user.get(user_id)


class FakeDocumentService:
    def __init__(self, content_model=None):
        self.content_model = content_model or dict(EMPTY_CONTENT_MODEL)
        self.saved_versions = []

    def get_content_for_node(self, node, document, version_no=None):
        import json

        version = _Version(uuid.uuid4(), 1)
        return node, document, version, json.dumps(self.content_model).encode("utf-8")

    def add_version_for_node(
        self, node, document, data, *, actor_user_id=None, **kwargs
    ):
        version = _Version(uuid.uuid4(), 2)
        self.saved_versions.append((node, document, data, actor_user_id))
        return version


class FakeAiService:
    def __init__(self, reply="proposed", slug="default"):
        self.reply = reply
        self.slug = slug
        self.calls = []

    def run_capability(self, user_id, node_id, capability_id, **kwargs):
        self.calls.append((user_id, node_id, capability_id, kwargs))
        return self.reply, self.slug


def _build_service(access_by_user, document, node_kind="document"):
    node_id = uuid.uuid4()
    node = _Node(node_id, kind=node_kind)
    node_repository = FakeNodeRepository({node_id: node})
    document_repository = FakeDocumentRepository({node_id: document})
    version_repository = FakeVersionRepository({})
    access_resolver = FakeAccessResolver(access_by_user)
    document_service = FakeDocumentService()
    edit_lease_service = EditLeaseService(_FakeLeaseRepository())
    ai_service = FakeAiService()

    service = OfficeDocEditorService(
        node_repository=node_repository,
        document_repository=document_repository,
        version_repository=version_repository,
        document_service=document_service,
        access_resolver=access_resolver,
        edit_lease_service=edit_lease_service,
        ai_service=ai_service,
    )
    return service, node_id, document_service, ai_service


class _FakeLeaseRepository:
    def __init__(self):
        self._leases = {}

    def find_by_node_id(self, node_id):
        return self._leases.get(node_id)

    def upsert(self, node_id, holder_user_id, acquired_at, expires_at):
        class _Row:
            pass

        row = self._leases.setdefault(node_id, _Row())
        row.holder_user_id = holder_user_id
        row.acquired_at = acquired_at
        row.expires_at = expires_at
        return row

    def delete(self, node_id):
        self._leases.pop(node_id, None)


def test_open_document_returns_the_content_model_and_lease_state():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _document_service, _ai = _build_service(
        {owner_id: ACCESS_OWNER}, document
    )

    view = service.open_document(owner_id, node_id)

    assert view.content == EMPTY_CONTENT_MODEL
    assert view.access == ACCESS_OWNER
    assert view.lease.held is False


def test_open_document_raises_not_found_for_a_non_text_document():
    owner_id = uuid.uuid4()
    document = _Document(doc_type="file")
    service, node_id, _document_service, _ai = _build_service(
        {owner_id: ACCESS_OWNER}, document
    )

    with pytest.raises(OfficeNodeNotFoundError):
        service.open_document(owner_id, node_id)


def test_save_with_the_correct_base_version_no_succeeds():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=None)
    service, node_id, document_service, _ai = _build_service(
        {owner_id: ACCESS_OWNER}, document
    )

    service.save_document(owner_id, node_id, dict(EMPTY_CONTENT_MODEL), 0)

    assert len(document_service.saved_versions) == 1


def test_save_with_a_stale_base_version_no_raises_and_writes_nothing():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=None)
    service, node_id, document_service, _ai = _build_service(
        {owner_id: ACCESS_OWNER}, document
    )

    with pytest.raises(OfficeDocStaleVersionError):
        service.save_document(owner_id, node_id, dict(EMPTY_CONTENT_MODEL), 999)

    assert document_service.saved_versions == []


def test_view_only_access_cannot_save():
    owner_id, viewer_id = uuid.uuid4(), uuid.uuid4()
    document = _Document(current_version_id=None)
    service, node_id, document_service, _ai = _build_service(
        {owner_id: ACCESS_OWNER, viewer_id: "view"}, document
    )

    with pytest.raises(OfficeShareForbiddenError):
        service.save_document(viewer_id, node_id, dict(EMPTY_CONTENT_MODEL), 0)

    assert document_service.saved_versions == []


def test_view_only_access_cannot_acquire_a_lease():
    owner_id, viewer_id = uuid.uuid4(), uuid.uuid4()
    document = _Document()
    service, node_id, _document_service, _ai = _build_service(
        {owner_id: ACCESS_OWNER, viewer_id: "view"}, document
    )

    with pytest.raises(OfficeShareForbiddenError):
        service.acquire_lease(viewer_id, node_id)


def test_save_while_locked_by_another_editor_raises():
    owner_id, editor_id = uuid.uuid4(), uuid.uuid4()
    document = _Document(current_version_id=None)
    service, node_id, document_service, _ai = _build_service(
        {owner_id: ACCESS_OWNER, editor_id: "edit"}, document
    )
    service.acquire_lease(owner_id, node_id)

    from plugins.office.office.services.exceptions import OfficeDocLockedError

    with pytest.raises(OfficeDocLockedError):
        service.save_document(editor_id, node_id, dict(EMPTY_CONTENT_MODEL), 0)
    assert document_service.saved_versions == []


def test_ai_capability_rejected_when_ai_disabled_on_the_document():
    owner_id = uuid.uuid4()
    document = _Document(ai_enabled=False)
    service, node_id, _document_service, ai_service = _build_service(
        {owner_id: ACCESS_OWNER}, document
    )

    with pytest.raises(OfficeAiDisabledError):
        service.run_ai_capability(owner_id, node_id, "summarize", selection_text="hi")
    assert ai_service.calls == []


def test_ai_capability_runs_when_ai_enabled_on_the_document():
    owner_id = uuid.uuid4()
    document = _Document(ai_enabled=True)
    service, node_id, _document_service, ai_service = _build_service(
        {owner_id: ACCESS_OWNER}, document
    )

    result = service.run_ai_capability(
        owner_id, node_id, "summarize", selection_text="hi"
    )
    assert result["proposed_text"] == "proposed"
    assert len(ai_service.calls) == 1


def test_ai_capability_forwards_a_prompt_to_the_ai_service():
    owner_id = uuid.uuid4()
    document = _Document(ai_enabled=True)
    service, node_id, _document_service, ai_service = _build_service(
        {owner_id: ACCESS_OWNER}, document
    )

    service.run_ai_capability(
        owner_id,
        node_id,
        "freeform",
        selection_text="",
        prompt="rewrite this as three bullet points",
    )
    assert ai_service.calls[0][3]["prompt"] == "rewrite this as three bullet points"


def test_only_the_owner_may_flip_ai_enabled_via_save():
    owner_id, editor_id = uuid.uuid4(), uuid.uuid4()
    document = _Document(current_version_id=None, ai_enabled=False)
    service, node_id, _document_service, _ai = _build_service(
        {owner_id: ACCESS_OWNER, editor_id: "edit"}, document
    )

    service.save_document(
        editor_id, node_id, dict(EMPTY_CONTENT_MODEL), 0, ai_enabled=True
    )
    assert document.ai_enabled is False  # a non-owner's flag change is ignored

    service.release_lease(editor_id, node_id)
    service.save_document(
        owner_id, node_id, dict(EMPTY_CONTENT_MODEL), 0, ai_enabled=True
    )
    assert document.ai_enabled is True
