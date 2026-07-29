"""``OfficeDocAssetService`` — image asset upload/serving for VBWD Docs
(S147-3 toolbar bundle: "insert image").

Images pasted or inserted into a Doc upload as ORDINARY ``office_document``
files, exactly like any other vault upload (DRY — the byte-level write goes
through the SAME ``OfficeDocumentService.upload_document`` path S147-1
already tested: node/document/version creation, quota, mime sniffing). They
land in one hidden ``_assets`` folder per owner rather than a second upload
surface — one storage path, one quota, one ACL (epic doc).

Access to READ an asset is scoped tighter than "you can see this document
kind": a viewer with ``view`` access to Doc A must not be able to guess the
node id of an image that only ever appeared in Doc B, even though both were
uploaded by the same owner into the same physical folder — so
:meth:`get_asset` additionally requires the asset id to be referenced by an
``image`` node in the Doc's own CURRENT content model
(``doc_content.collect_referenced_asset_node_ids``), not merely "owned by
the same user".
"""
from __future__ import annotations

from typing import Optional, Tuple

from vbwd.utils.datetime_utils import utcnow

from plugins.office.office.models.office_document import DOC_TYPE_FILE, DOC_TYPE_TEXT
from plugins.office.office.models.office_node import NODE_KIND_FOLDER
from plugins.office.office.services.doc_content import (
    IMAGE_SRC_PREFIX,
    RESERVED_ASSETS_FOLDER_NAME,
    collect_referenced_asset_node_ids,
    parse_content_bytes,
)
from plugins.office.office.services.doc_editor_service import EDIT_CAPABLE_ACCESS
from plugins.office.office.services.editable_node_resolver import (
    resolve_editable_document,
)
from plugins.office.office.services.exceptions import (
    OfficeDocAssetInvalidError,
    OfficeNodeNotFoundError,
    OfficeShareForbiddenError,
)
from plugins.office.office.services.mime_sniffer import MimeSniffer

#: Mime types accepted for an inserted image — the server-sniffed value,
#: never the client's declared Content-Type (mirrors B3's rule elsewhere in
#: this plugin).
ALLOWED_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)


class OfficeDocAssetService:
    def __init__(
        self,
        node_repository,
        document_repository,
        document_service,
        access_resolver,
        mime_sniffer: Optional[MimeSniffer] = None,
    ) -> None:
        self._node_repository = node_repository
        self._document_repository = document_repository
        self._document_service = document_service
        self._access_resolver = access_resolver
        self._mime_sniffer = mime_sniffer or MimeSniffer()

    def upload_asset(self, user_id, doc_node_id, filename: str, data: bytes) -> dict:
        doc_node, _document, access = self._resolve_doc(user_id, doc_node_id)
        if access not in EDIT_CAPABLE_ACCESS:
            raise OfficeShareForbiddenError(doc_node_id)

        mime_type = self._mime_sniffer.sniff(data)
        if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
            raise OfficeDocAssetInvalidError(f"Unsupported image type: {mime_type!r}")

        folder = self._resolve_or_create_assets_folder(doc_node.owner_user_id)
        asset_node, _asset_document, _version = self._upload_into_hidden_folder(
            doc_node.owner_user_id, filename, folder, data
        )
        return {
            "node_id": str(asset_node.id),
            "src": f"{IMAGE_SRC_PREFIX}{asset_node.id}",
        }

    def _upload_into_hidden_folder(
        self, owner_user_id, filename: str, folder, data: bytes
    ):
        """Upload into the hidden ``_assets`` folder.

        ``OfficeDocumentService.upload_document`` resolves its ``parent_id``
        through ``OfficeNodeAccess.resolve_parent_folder``, which correctly
        REFUSES a trashed parent — the right rule for a user's own trashed
        folder — but the ``_assets`` folder is deliberately kept trashed at
        rest (``_resolve_or_create_assets_folder``'s docstring) to hide it
        from the ordinary listing. Left as-is, every single upload into it
        would 404. The folder's hidden flag is lifted for the moment of this
        call and restored immediately after, so it stays hidden between
        requests while still being usable as an upload target.
        """
        folder.trashed_at = None
        self._node_repository.add(folder)
        try:
            return self._document_service.upload_document(
                owner_user_id, filename, folder.id, data, doc_type=DOC_TYPE_FILE
            )
        finally:
            folder.trashed_at = utcnow()
            self._node_repository.add(folder)

    def get_asset(
        self, user_id, doc_node_id, asset_node_id
    ) -> Tuple[object, object, object, bytes]:
        doc_node, document, _access = self._resolve_doc(user_id, doc_node_id)

        _n, _d, _version, content_bytes = self._document_service.get_content_for_node(
            doc_node, document
        )
        referenced_ids = collect_referenced_asset_node_ids(
            parse_content_bytes(content_bytes)
        )
        if str(asset_node_id) not in referenced_ids:
            raise OfficeNodeNotFoundError(asset_node_id)

        asset_node = self._node_repository.find_by_id_for_owner(
            asset_node_id, doc_node.owner_user_id
        )
        if asset_node is None:
            raise OfficeNodeNotFoundError(asset_node_id)
        asset_document = self._document_repository.find_by_node_id(asset_node.id)
        if asset_document is None:
            raise OfficeNodeNotFoundError(asset_node_id)

        return self._document_service.get_content_for_node(asset_node, asset_document)

    def _resolve_doc(self, user_id, doc_node_id):
        return resolve_editable_document(
            self._access_resolver,
            self._node_repository,
            self._document_repository,
            user_id,
            doc_node_id,
            allowed_doc_types=(DOC_TYPE_TEXT,),
        )

    def _resolve_or_create_assets_folder(self, owner_user_id):
        for node in self._node_repository.find_children(
            owner_user_id, None, include_trashed=True
        ):
            if (
                node.kind == NODE_KIND_FOLDER
                and node.name == RESERVED_ASSETS_FOLDER_NAME
            ):
                return node

        folder = self._document_service.create_folder(
            owner_user_id, RESERVED_ASSETS_FOLDER_NAME, parent_id=None
        )
        # Hidden from the ordinary vault listing (OfficeNodeRepository.
        # find_children filters on trashed_at IS NULL) while staying
        # individually addressable by id — see doc_content.py's docstring
        # on RESERVED_ASSETS_FOLDER_NAME for why this reuses trashed_at
        # rather than a new column. No route ever offers a mass "empty
        # trash" purge that could sweep this folder up.
        folder.trashed_at = utcnow()
        return self._node_repository.add(folder)
