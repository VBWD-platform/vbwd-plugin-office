"""``OfficeDocumentService`` — the vault's orchestrator (S147-1).

Owns folder create/list, upload, new-version, restore, rename/move,
trash/purge — the "document/node service" the sprint calls for. Depends only
on the abstractions it needs (repositories, ``DocumentStore``,
``QuotaService``, ``MimeSniffer``) — never on Flask, never on the DB session
directly (DIP); routes.py is the only layer that talks HTTP.

Quota is checked BEFORE a write (cheap, avoids wasted I/O) and AFTER it
(B2 — a stream can exceed what a declared length promised, or a concurrent
upload can race the pre-check); a post-write breach deletes the just-written
blob and raises, so nothing is left orphaned or half-accounted. Restoring a
version is a pointer swap: the new version row reuses an EXISTING
``storage_key`` (nothing is re-written, so it costs no extra quota).
"""
from dataclasses import dataclass
from typing import List, Optional

from vbwd.utils.datetime_utils import utcnow

from plugins.office.office.models.office_document import DOC_TYPE_FILE, OfficeDocument
from plugins.office.office.models.office_node import (
    NODE_KIND_DOCUMENT,
    NODE_KIND_FOLDER,
    OfficeNode,
)
from plugins.office.office.models.office_version import OfficeVersion
from plugins.office.office.services.exceptions import (
    OfficeNodeNotFoundError,
    OfficeUploadTooLargeError,
    OfficeVersionNotFoundError,
)
from plugins.office.office.services.name_sanitizer import sanitize_display_name
from plugins.office.office.services.node_access import OfficeNodeAccess
from plugins.office.office.services.quota_service import OfficeQuotaExceededError
from plugins.office.office.storage.document_store import DocumentStore

#: Sentinel distinguishing "the caller did not mention parent_id" (leave
#: unchanged) from an explicit ``parent_id=None`` (move to the vault root).
PARENT_ID_UNSET = object()

#: Sentinel distinguishing "the caller did not pass actor_user_id" (default
#: to the quota-charged user, the pre-S147-2 behaviour) from an explicit
#: ``actor_user_id=None`` (S147-2 C4 — a purely anonymous share edit).
ACTOR_USER_ID_UNSET = object()

#: Finder-vocabulary suffix a duplicate's name gets on a collision
#: ("report.txt" -> "report copy.txt" -> "report copy 2.txt").
COPY_NAME_SUFFIX = "copy"


@dataclass(frozen=True)
class NodeSummary:
    """One entry of a folder listing — a node, plus document facts when the
    node is a document (kept a plain projection so routes never touch the
    ORM objects directly)."""

    node: OfficeNode
    document: Optional[OfficeDocument]

    def to_dict(self) -> dict:
        payload = self.node.to_dict()
        if self.document is not None:
            payload["document"] = {
                "id": str(self.document.id),
                "doc_type": self.document.doc_type,
                "mime_type": self.document.mime_type,
                "size_bytes": self.document.size_bytes,
            }
        return payload


class OfficeDocumentService:
    def __init__(
        self,
        node_repository,
        document_repository,
        version_repository,
        quota_service,
        document_store: DocumentStore,
        mime_sniffer,
        max_upload_bytes: int,
    ) -> None:
        self._node_repository = node_repository
        self._document_repository = document_repository
        self._version_repository = version_repository
        self._quota_service = quota_service
        self._document_store = document_store
        self._mime_sniffer = mime_sniffer
        self._max_upload_bytes = max_upload_bytes
        self._node_access = OfficeNodeAccess(node_repository)

    # ------------------------------------------------------------------
    # Folders + listing
    # ------------------------------------------------------------------

    def create_folder(self, owner_user_id, name: str, parent_id=None) -> OfficeNode:
        parent = self._node_access.resolve_parent_folder(owner_user_id, parent_id)
        node = OfficeNode(
            owner_user_id=owner_user_id,
            parent_id=parent.id if parent else None,
            kind=NODE_KIND_FOLDER,
            name=sanitize_display_name(name),
        )
        return self._node_repository.add(node)

    def list_children(self, owner_user_id, parent_id=None) -> List[NodeSummary]:
        self._node_access.resolve_parent_folder(owner_user_id, parent_id)
        nodes = self._node_repository.find_children(owner_user_id, parent_id)
        summaries = []
        for node in nodes:
            document = None
            if node.kind == NODE_KIND_DOCUMENT:
                document = self._document_repository.find_by_node_id(node.id)
            summaries.append(NodeSummary(node=node, document=document))
        return summaries

    # ------------------------------------------------------------------
    # Upload / versions
    # ------------------------------------------------------------------

    def upload_document(
        self,
        owner_user_id,
        name: str,
        parent_id,
        data: bytes,
        doc_type: str = DOC_TYPE_FILE,
    ):
        self._enforce_upload_cap(data)
        parent = self._node_access.resolve_parent_folder(owner_user_id, parent_id)
        self._quota_service.ensure_capacity(owner_user_id, len(data))  # BEFORE (B2)

        node = self._node_repository.add(
            OfficeNode(
                owner_user_id=owner_user_id,
                parent_id=parent.id if parent else None,
                kind=NODE_KIND_DOCUMENT,
                name=sanitize_display_name(name),
            )
        )
        document = self._document_repository.add(
            OfficeDocument(
                node_id=node.id,
                doc_type=doc_type,
                mime_type=self._mime_sniffer.sniff(data),
                size_bytes=0,
                sealed_data_key=DocumentStore.generate_sealed_data_key(),
            )
        )

        version = self._write_version(owner_user_id, document, version_no=1, data=data)
        return node, document, version

    def add_version(self, owner_user_id, node_id, data: bytes):
        _node, document = self._require_document(owner_user_id, node_id)
        return self.add_version_for_node(
            _node, document, data, actor_user_id=owner_user_id
        )

    def add_version_for_node(
        self,
        node: OfficeNode,
        document: OfficeDocument,
        data: bytes,
        *,
        actor_user_id=ACTOR_USER_ID_UNSET,
        created_by_share_id=None,
        quota_owner_user_id=None,
    ):
        """Add a new version for an ALREADY-RESOLVED ``node``/``document`` —
        the share-aware entry point (S147-2): unlike :meth:`add_version`,
        this never re-resolves ownership (a share-based edit is, by
        definition, not the owner), so the caller (``SharingService``) is
        responsible for having proven access via ``AccessResolver`` first.

        Quota is always charged to ``quota_owner_user_id`` (defaulting to
        ``node.owner_user_id`` — C5: an anonymous edit charges the OWNER's
        quota, never the editor's, because the editor may not even have an
        account). ``actor_user_id`` defaults to the quota-charged user
        (the pre-S147-2, owner-authenticated behaviour); pass it explicitly
        (including ``None`` for a purely anonymous edit) to attribute the
        version correctly.
        """
        self._enforce_upload_cap(data)
        charge_user_id = quota_owner_user_id or node.owner_user_id
        self._quota_service.ensure_capacity(charge_user_id, len(data))  # BEFORE (B2)

        document.mime_type = self._mime_sniffer.sniff(data)
        next_version_no = self._version_repository.next_version_no(document.id)
        version = self._write_version(
            charge_user_id,
            document,
            next_version_no,
            data,
            actor_user_id=actor_user_id,
            created_by_share_id=created_by_share_id,
        )
        return version

    def _write_version(
        self,
        charge_user_id,
        document: OfficeDocument,
        version_no: int,
        data: bytes,
        *,
        actor_user_id=ACTOR_USER_ID_UNSET,
        created_by_share_id=None,
    ):
        if actor_user_id is ACTOR_USER_ID_UNSET:
            actor_user_id = charge_user_id
        blob = self._document_store.write_new_version(
            charge_user_id, document.id, version_no, document.sealed_data_key, data
        )
        version = self._version_repository.add(
            OfficeVersion(
                document_id=document.id,
                version_no=version_no,
                storage_key=blob.storage_key,
                size_bytes=blob.size_bytes,
                sha256=blob.sha256,
                created_by_user_id=actor_user_id,
                created_by_share_id=created_by_share_id,
            )
        )
        document.size_bytes = blob.size_bytes
        document.current_version_id = version.id
        self._document_repository.add(document)

        self._quota_service.apply_delta(charge_user_id, blob.size_bytes)
        self._enforce_post_write_quota(charge_user_id, blob.storage_key)  # AFTER (B2)
        return version

    def _enforce_upload_cap(self, data: bytes) -> None:
        if len(data) > self._max_upload_bytes:
            raise OfficeUploadTooLargeError(len(data), self._max_upload_bytes)

    def _enforce_post_write_quota(self, owner_user_id, storage_key: str) -> None:
        try:
            self._quota_service.raise_if_over_quota(owner_user_id)
        except OfficeQuotaExceededError:
            # The pre-check passed but the committed total now exceeds quota
            # (a race with a concurrent write). Undo the blob just written —
            # nothing has been committed to the DB yet, so the route's
            # teardown-without-commit discards the rows on its own.
            self._document_store.delete(storage_key)
            raise

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_document_metadata(self, owner_user_id, node_id) -> dict:
        node, document = self._require_document(owner_user_id, node_id)
        return self.get_document_metadata_for_node(node, document)

    def get_document_metadata_for_node(
        self, node: OfficeNode, document: OfficeDocument
    ) -> dict:
        """Share-aware entry point (S147-2) — same projection as
        :meth:`get_document_metadata`, for an already-resolved node."""
        current_version = None
        if document.current_version_id is not None:
            current_version = self._version_repository.find_by_id(
                document.current_version_id
            )
        payload = node.to_dict()
        payload["document"] = document.to_dict()
        payload["current_version_no"] = (
            current_version.version_no if current_version else None
        )
        return payload

    def list_versions(self, owner_user_id, node_id) -> List[OfficeVersion]:
        _, document = self._require_document(owner_user_id, node_id)
        return self._version_repository.find_by_document_id(document.id)

    def get_content(self, owner_user_id, node_id, version_no: Optional[int] = None):
        node, document = self._require_document(owner_user_id, node_id)
        return self.get_content_for_node(node, document, version_no)

    def get_content_for_node(
        self,
        node: OfficeNode,
        document: OfficeDocument,
        version_no: Optional[int] = None,
    ):
        """Share-aware entry point (S147-2) — same read as :meth:`get_content`,
        for an already-resolved node/document."""
        version = self._resolve_version(document, version_no)
        plaintext = self._document_store.read(
            version.storage_key, document.sealed_data_key, version.sha256
        )
        return node, document, version, plaintext

    def _resolve_version(self, document: OfficeDocument, version_no: Optional[int]):
        if version_no is None:
            version = (
                self._version_repository.find_by_id(document.current_version_id)
                if document.current_version_id
                else None
            )
        else:
            version = self._version_repository.find_by_document_and_version_no(
                document.id, version_no
            )
        if version is None:
            raise OfficeVersionNotFoundError(document.node_id)
        return version

    def restore_version(self, owner_user_id, node_id, version_no: int) -> OfficeVersion:
        _, document = self._require_document(owner_user_id, node_id)
        target = self._version_repository.find_by_document_and_version_no(
            document.id, version_no
        )
        if target is None:
            raise OfficeVersionNotFoundError(node_id)

        next_version_no = self._version_repository.next_version_no(document.id)
        restored = self._version_repository.add(
            OfficeVersion(
                document_id=document.id,
                version_no=next_version_no,
                storage_key=target.storage_key,  # pointer swap — no new blob
                size_bytes=target.size_bytes,
                sha256=target.sha256,
                created_by_user_id=owner_user_id,
            )
        )
        document.size_bytes = restored.size_bytes
        document.current_version_id = restored.id
        self._document_repository.add(document)
        return restored

    # ------------------------------------------------------------------
    # Rename / move / trash / purge
    # ------------------------------------------------------------------

    def rename_or_move(
        self, owner_user_id, node_id, name=None, parent_id=PARENT_ID_UNSET
    ) -> OfficeNode:
        node = self._node_access.require_owned_node(owner_user_id, node_id)
        if name is not None:
            node.name = sanitize_display_name(name)
        if parent_id is not PARENT_ID_UNSET:
            new_parent = self._node_access.resolve_parent_folder(
                owner_user_id, parent_id
            )
            if new_parent is not None:
                self._node_access.guard_against_cycle(node, new_parent)
            node.parent_id = new_parent.id if new_parent else None
        return self._node_repository.add(node)

    def copy_node(self, owner_user_id, node_id, parent_id=None) -> OfficeNode:
        """Duplicate ``node_id`` into ``parent_id`` (the vault root when
        falsy). A folder copy is recursive; a document copy re-writes its
        current version's bytes through :meth:`upload_document` so quota is
        charged and MIME/size bookkeeping happens exactly once (DRY — no
        hand-written rows). Name collisions in the destination get a Finder-
        style " copy" / " copy 2" suffix.

        For a folder, the WHOLE subtree's byte total is quota-checked up
        front, before anything is written — a copy that would not fit is
        refused as a whole (413) rather than left half-written."""
        source = self._node_access.require_owned_node(owner_user_id, node_id)
        destination_parent = self._node_access.resolve_parent_folder(
            owner_user_id, parent_id
        )
        if source.kind == NODE_KIND_FOLDER and destination_parent is not None:
            self._node_access.guard_against_cycle(source, destination_parent)

        destination_parent_id = destination_parent.id if destination_parent else None
        if source.kind == NODE_KIND_FOLDER:
            subtree_bytes = self._subtree_size_bytes(owner_user_id, source)
            self._quota_service.ensure_capacity(owner_user_id, subtree_bytes)

        copy_name = self._unique_copy_name(
            owner_user_id, destination_parent_id, source.name
        )
        return self._copy_node_recursive(
            owner_user_id, source, destination_parent_id, copy_name
        )

    def _copy_node_recursive(
        self, owner_user_id, source_node: OfficeNode, destination_parent_id, name: str
    ) -> OfficeNode:
        if source_node.kind == NODE_KIND_FOLDER:
            return self._copy_folder(
                owner_user_id, source_node, destination_parent_id, name
            )
        return self._copy_document(
            owner_user_id, source_node, destination_parent_id, name
        )

    def _copy_folder(
        self, owner_user_id, source_node: OfficeNode, destination_parent_id, name: str
    ) -> OfficeNode:
        new_folder = self._node_repository.add(
            OfficeNode(
                owner_user_id=owner_user_id,
                parent_id=destination_parent_id,
                kind=NODE_KIND_FOLDER,
                name=name,
            )
        )
        for child in self._node_repository.find_children(owner_user_id, source_node.id):
            self._copy_node_recursive(owner_user_id, child, new_folder.id, child.name)
        return new_folder

    def _copy_document(
        self, owner_user_id, source_node: OfficeNode, destination_parent_id, name: str
    ) -> OfficeNode:
        source_document = self._document_repository.find_by_node_id(source_node.id)
        if source_document is None:
            raise OfficeNodeNotFoundError(source_node.id)
        _, _, _, content = self.get_content_for_node(source_node, source_document)
        new_node, _new_document, _new_version = self.upload_document(
            owner_user_id,
            name,
            destination_parent_id,
            content,
            doc_type=source_document.doc_type,
        )
        return new_node

    def _subtree_size_bytes(self, owner_user_id, folder_node: OfficeNode) -> int:
        total_bytes = 0
        for child in self._node_repository.find_children(owner_user_id, folder_node.id):
            if child.kind == NODE_KIND_FOLDER:
                total_bytes += self._subtree_size_bytes(owner_user_id, child)
                continue
            child_document = self._document_repository.find_by_node_id(child.id)
            if child_document is not None:
                total_bytes += child_document.size_bytes
        return total_bytes

    def _unique_copy_name(self, owner_user_id, parent_id, original_name: str) -> str:
        """The original name unchanged, UNLESS the destination already has a
        sibling with that exact name (a same-folder duplicate always does) —
        only then does Finder-style " copy" / " copy 2" suffixing kick in."""
        sibling_names = {
            sibling.name
            for sibling in self._node_repository.find_children(owner_user_id, parent_id)
        }
        if original_name not in sibling_names:
            return original_name
        stem, extension = self._split_stem_and_extension(original_name)
        candidate = f"{stem} {COPY_NAME_SUFFIX}{extension}"
        attempt = 2
        while candidate in sibling_names:
            candidate = f"{stem} {COPY_NAME_SUFFIX} {attempt}{extension}"
            attempt += 1
        return sanitize_display_name(candidate)

    @staticmethod
    def _split_stem_and_extension(name: str):
        stem, separator, extension = name.rpartition(".")
        if not separator or not stem:
            return name, ""
        return stem, f".{extension}"

    def trash_node(self, owner_user_id, node_id) -> OfficeNode:
        node = self._node_access.require_owned_node(owner_user_id, node_id)
        node.trashed_at = utcnow()
        return self._node_repository.add(node)

    def purge_node(self, owner_user_id, node_id) -> None:
        node = self._node_access.require_owned_node(owner_user_id, node_id)
        self._purge_recursive(owner_user_id, node)

    def _purge_recursive(self, owner_user_id, node: OfficeNode) -> None:
        if node.kind == NODE_KIND_FOLDER:
            for child in self._node_repository.find_children(
                owner_user_id, node.id, include_trashed=True
            ):
                self._purge_recursive(owner_user_id, child)
            self._node_repository.delete(node)
            return

        document = self._document_repository.find_by_node_id(node.id)
        if document is None:
            self._node_repository.delete(node)
            return
        self._purge_document_blobs(owner_user_id, document)
        self._node_repository.delete(node)  # cascades document + version rows

    def _purge_document_blobs(self, owner_user_id, document: OfficeDocument) -> None:
        seen_storage_keys = set()
        freed_bytes = 0
        for version in self._version_repository.find_by_document_id(document.id):
            if version.storage_key in seen_storage_keys:
                continue  # a restored version REUSES a storage key (no double-free)
            seen_storage_keys.add(version.storage_key)
            self._document_store.delete(version.storage_key)
            freed_bytes += version.size_bytes
        if freed_bytes:
            self._quota_service.apply_delta(owner_user_id, -freed_bytes)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require_document(self, owner_user_id, node_id):
        node = self._node_access.require_owned_node(owner_user_id, node_id)
        if node.kind != NODE_KIND_DOCUMENT:
            raise OfficeNodeNotFoundError(node_id)
        document = self._document_repository.find_by_node_id(node.id)
        if document is None:
            raise OfficeNodeNotFoundError(node_id)
        return node, document
