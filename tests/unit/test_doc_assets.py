"""S147-3 toolbar bundle unit coverage for ``OfficeDocAssetService`` —
"insert image": upload lands in the hidden ``_assets`` folder and returns an
``office-asset:`` src (never a URL); reading an asset back is refused unless
it is actually referenced by the requesting Doc's OWN current content (the
ACL boundary the module's docstring describes) — verified here, not assumed.
Against in-memory fakes (no DB), mirroring ``test_doc_editor_service.py``'s
fake style.
"""
import json
import uuid

import pytest

from plugins.office.office.models.office_document import DOC_TYPE_FILE, DOC_TYPE_TEXT
from plugins.office.office.models.office_node import (
    NODE_KIND_DOCUMENT,
    NODE_KIND_FOLDER,
)
from plugins.office.office.models.office_share import PERMISSION_VIEW
from plugins.office.office.services.access_resolver import ACCESS_OWNER
from plugins.office.office.services.doc_assets import OfficeDocAssetService
from plugins.office.office.services.doc_content import EMPTY_CONTENT_MODEL
from plugins.office.office.services.exceptions import (
    OfficeDocAssetInvalidError,
    OfficeNodeNotFoundError,
    OfficeShareForbiddenError,
)

# A real PNG magic-number prefix — enough for MimeSniffer to sniff
# "image/png" without needing a fully valid image body.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-body"
PLAIN_TEXT_BYTES = b"hello, this is not an image"


class _Node:
    def __init__(self, node_id, owner_user_id, kind, name, parent_id=None):
        self.id = node_id
        self.owner_user_id = owner_user_id
        self.kind = kind
        self.name = name
        self.parent_id = parent_id
        self.trashed_at = None


class _Document:
    def __init__(self, document_id, node_id, doc_type, current_version_id=None):
        self.id = document_id
        self.node_id = node_id
        self.doc_type = doc_type
        self.current_version_id = current_version_id


class FakeNodeRepository:
    def __init__(self):
        self._nodes = {}

    def add(self, node):
        self._nodes[node.id] = node
        return node

    def find_by_id(self, node_id):
        return self._nodes.get(node_id)

    def find_by_id_for_owner(self, node_id, owner_user_id):
        # Mirrors the real repository accepting either a UUID or its string
        # form (SQLAlchemy coerces a str into the UUID column type) — the
        # asset service passes the string it stored in the upload response.
        node = self._nodes.get(node_id) or self._nodes.get(uuid.UUID(str(node_id)))
        if node is None or node.owner_user_id != owner_user_id:
            return None
        return node

    def find_children(self, owner_user_id, parent_id, include_trashed=False):
        return [
            node
            for node in self._nodes.values()
            if node.owner_user_id == owner_user_id and node.parent_id == parent_id
        ]


class FakeDocumentRepository:
    def __init__(self):
        self._documents_by_node_id = {}

    def add(self, document):
        self._documents_by_node_id[document.node_id] = document
        return document

    def find_by_node_id(self, node_id):
        return self._documents_by_node_id.get(node_id)


class FakeAccessResolver:
    def __init__(self, access_by_user):
        self._access_by_user = access_by_user

    def resolve(self, node_id, *, user_id=None, share_token=None):
        return self._access_by_user.get(user_id)


class FakeDocumentService:
    """Models JUST enough of ``OfficeDocumentService`` for the asset
    service's calls: folder creation, a byte-content upload, and reading a
    node/document's current content back."""

    def __init__(self, node_repository, document_repository):
        self._node_repository = node_repository
        self._document_repository = document_repository
        self._content_by_document_id = {}

    def create_folder(self, owner_user_id, name, parent_id=None):
        node = _Node(uuid.uuid4(), owner_user_id, NODE_KIND_FOLDER, name, parent_id)
        return self._node_repository.add(node)

    def upload_document(
        self, owner_user_id, name, parent_id, data, doc_type=DOC_TYPE_FILE
    ):
        node = _Node(uuid.uuid4(), owner_user_id, NODE_KIND_DOCUMENT, name, parent_id)
        self._node_repository.add(node)
        document = _Document(uuid.uuid4(), node.id, doc_type)
        self._document_repository.add(document)
        self._content_by_document_id[document.id] = data
        version = _Document(uuid.uuid4(), node.id, doc_type)  # id stand-in
        return node, document, version

    def get_content_for_node(self, node, document, version_no=None):
        data = self._content_by_document_id.get(document.id, b"")
        return node, document, None, data

    def set_content(self, document, data: bytes) -> None:
        """Test-only helper — updates a document's current content, standing
        in for ``add_version_for_node`` (the asset service never calls it;
        only the Doc EDITOR writes Doc content)."""
        self._content_by_document_id[document.id] = data


@pytest.fixture
def owner_id():
    return uuid.uuid4()


@pytest.fixture
def wiring(owner_id):
    node_repository = FakeNodeRepository()
    document_repository = FakeDocumentRepository()
    document_service = FakeDocumentService(node_repository, document_repository)

    doc_node = _Node(uuid.uuid4(), owner_id, NODE_KIND_DOCUMENT, "My Doc")
    node_repository.add(doc_node)
    doc_document = _Document(uuid.uuid4(), doc_node.id, DOC_TYPE_TEXT)
    document_repository.add(doc_document)
    document_service.set_content(
        doc_document, json.dumps(EMPTY_CONTENT_MODEL).encode("utf-8")
    )

    return (
        node_repository,
        document_repository,
        document_service,
        doc_node,
        doc_document,
    )


def _build_service(
    node_repository, document_repository, document_service, access_by_user
):
    return OfficeDocAssetService(
        node_repository=node_repository,
        document_repository=document_repository,
        document_service=document_service,
        access_resolver=FakeAccessResolver(access_by_user),
    )


def test_upload_asset_returns_an_office_asset_src_not_a_url(owner_id, wiring):
    (
        node_repository,
        document_repository,
        document_service,
        doc_node,
        _doc_document,
    ) = wiring
    service = _build_service(
        node_repository, document_repository, document_service, {owner_id: ACCESS_OWNER}
    )

    result = service.upload_asset(owner_id, doc_node.id, "cat.png", PNG_BYTES)

    assert result["src"] == f"office-asset:{result['node_id']}"
    uuid.UUID(result["node_id"])  # is a real node id, not a bare filename


def test_upload_asset_lands_in_a_hidden_assets_folder(owner_id, wiring):
    (
        node_repository,
        document_repository,
        document_service,
        doc_node,
        _doc_document,
    ) = wiring
    service = _build_service(
        node_repository, document_repository, document_service, {owner_id: ACCESS_OWNER}
    )

    result = service.upload_asset(owner_id, doc_node.id, "cat.png", PNG_BYTES)

    asset_node = node_repository.find_by_id(uuid.UUID(result["node_id"]))
    folder = node_repository.find_by_id(asset_node.parent_id)
    assert folder.name == "_assets"
    assert folder.trashed_at is not None  # hidden from the ordinary listing


def test_upload_asset_rejects_a_non_image_upload(owner_id, wiring):
    (
        node_repository,
        document_repository,
        document_service,
        doc_node,
        _doc_document,
    ) = wiring
    service = _build_service(
        node_repository, document_repository, document_service, {owner_id: ACCESS_OWNER}
    )

    with pytest.raises(OfficeDocAssetInvalidError):
        service.upload_asset(owner_id, doc_node.id, "notes.txt", PLAIN_TEXT_BYTES)


def test_upload_asset_is_forbidden_for_a_view_only_share(owner_id, wiring):
    (
        node_repository,
        document_repository,
        document_service,
        doc_node,
        _doc_document,
    ) = wiring
    viewer_id = uuid.uuid4()
    service = _build_service(
        node_repository,
        document_repository,
        document_service,
        {owner_id: ACCESS_OWNER, viewer_id: PERMISSION_VIEW},
    )

    with pytest.raises(OfficeShareForbiddenError):
        service.upload_asset(viewer_id, doc_node.id, "cat.png", PNG_BYTES)


def test_get_asset_refuses_an_asset_not_referenced_by_the_documents_own_content(
    owner_id, wiring
):
    """The ACL boundary this module's docstring describes: an asset that
    exists (owned by the same user, in the same physical folder) but is not
    referenced by THIS document's current content must be refused."""
    (
        node_repository,
        document_repository,
        document_service,
        doc_node,
        _doc_document,
    ) = wiring
    service = _build_service(
        node_repository, document_repository, document_service, {owner_id: ACCESS_OWNER}
    )
    uploaded = service.upload_asset(owner_id, doc_node.id, "cat.png", PNG_BYTES)
    # The Doc's content was never updated to reference the uploaded asset.

    with pytest.raises(OfficeNodeNotFoundError):
        service.get_asset(owner_id, doc_node.id, uploaded["node_id"])


def test_get_asset_succeeds_once_the_document_references_it(owner_id, wiring):
    (
        node_repository,
        document_repository,
        document_service,
        doc_node,
        doc_document,
    ) = wiring
    service = _build_service(
        node_repository, document_repository, document_service, {owner_id: ACCESS_OWNER}
    )
    uploaded = service.upload_asset(owner_id, doc_node.id, "cat.png", PNG_BYTES)
    content_model = {
        "type": "doc",
        "content": [{"type": "image", "attrs": {"src": uploaded["src"]}}],
    }
    document_service.set_content(
        doc_document, json.dumps(content_model).encode("utf-8")
    )

    _node, _document, _version, data = service.get_asset(
        owner_id, doc_node.id, uploaded["node_id"]
    )

    assert data == PNG_BYTES


def test_get_asset_is_reachable_by_a_view_only_share_holder(owner_id, wiring):
    """Any resolvable access (down to view) may READ an inserted image —
    only the WRITE (upload_asset) path is edit-gated; this is what makes a
    shared Doc's images render for a view-only recipient."""
    (
        node_repository,
        document_repository,
        document_service,
        doc_node,
        doc_document,
    ) = wiring
    viewer_id = uuid.uuid4()
    owner_service = _build_service(
        node_repository, document_repository, document_service, {owner_id: ACCESS_OWNER}
    )
    uploaded = owner_service.upload_asset(owner_id, doc_node.id, "cat.png", PNG_BYTES)
    content_model = {
        "type": "doc",
        "content": [{"type": "image", "attrs": {"src": uploaded["src"]}}],
    }
    document_service.set_content(
        doc_document, json.dumps(content_model).encode("utf-8")
    )

    viewer_service = _build_service(
        node_repository,
        document_repository,
        document_service,
        {owner_id: ACCESS_OWNER, viewer_id: PERMISSION_VIEW},
    )
    _node, _document, _version, data = viewer_service.get_asset(
        viewer_id, doc_node.id, uploaded["node_id"]
    )

    assert data == PNG_BYTES
