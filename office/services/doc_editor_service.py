"""``OfficeDocEditorService`` — the S147-3 Doc orchestrator.

A Doc is an ``office_node`` of ``kind='document'`` / ``doc_type='text'`` —
this service deliberately does NOT re-implement node/version/quota
bookkeeping; it delegates every byte-level read/write to the EXISTING
``OfficeDocumentService`` (reused via its share-aware ``*_for_node`` methods,
S147-1/-2) and asks the ONE ``AccessResolver`` (S147-2, epic D5) for every
access decision. This class owns exactly the Doc-specific concerns the vault
does not already have: the JSON content model, the edit lease, and the AI
helper.

Doc editing is reached only through a SESSION (owner, or a named share
identified by the caller's own login) — never an anonymous share token. The
generic ``/public/<token>/content`` byte routes (S147-2) still work for a
Doc like any other file; they simply do not know about the structured
content model, the lease, or the AI helper. This mirrors how
``SharingService.get_shared_document_metadata`` already treats "shared with
me" as session-only, and keeps anonymous-link co-editing (lease + AI +
budget, for a caller the platform cannot even identify) out of this MVP's
scope — the epic's D4/D8 controls are keyed on a real user id throughout.

The edit-LEASE methods below (``acquire_lease``/``release_lease``/
``presence``) are content-agnostic and are reused VERBATIM by VBWD
Spreadsheets (S147-4): they resolve any node whose ``doc_type`` is ``text``
OR ``sheet``, so the Sheets grid calls the exact same
``/docs/<node_id>/lease*`` routes as the Docs editor — one home per
behaviour (the sprint's own note on sharing lease/export routes), no second
lease mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from plugins.office.office.models.office_node import OfficeNode
from plugins.office.office.models.office_document import (
    DOC_TYPE_SHEET,
    DOC_TYPE_TEXT,
    OfficeDocument,
)
from plugins.office.office.services.access_resolver import ACCESS_OWNER
from plugins.office.office.services.doc_content import (
    empty_content_bytes,
    parse_content_bytes,
    serialize_content_model,
    validate_content_model,
)
from plugins.office.office.services.edit_lease_service import LeaseState
from plugins.office.office.services.editable_node_resolver import (
    current_version_no,
    resolve_editable_document,
)
from plugins.office.office.services.exceptions import (
    OfficeAiDisabledError,
    OfficeDocStaleVersionError,
    OfficeShareForbiddenError,
)
from plugins.office.office.models.office_share import PERMISSION_EDIT

#: Doc types the edit-lease methods accept — content-agnostic, shared with
#: Sheets (S147-4). Content-specific methods (open/save/AI) stay
#: ``DOC_TYPE_TEXT``-only via :meth:`_resolve_text_document`.
LOCKABLE_DOC_TYPES = (DOC_TYPE_TEXT, DOC_TYPE_SHEET)

#: Access levels that may save content or hold the edit lease (owner or an
#: edit-permission share) — matches the sprint's "edit to save or hold a
#: lease, view to read".
EDIT_CAPABLE_ACCESS = frozenset({ACCESS_OWNER, PERMISSION_EDIT})


@dataclass(frozen=True)
class DocView:
    """The full payload for ``GET``/``POST .../docs`` — model, version,
    access, and lease state in one response, per the sprint's API table."""

    node: OfficeNode
    document: OfficeDocument
    content: Dict[str, Any]
    version_no: int
    access: str
    lease: LeaseState

    def to_dict(self) -> dict:
        payload = self.node.to_dict()
        payload["document"] = self.document.to_dict()
        payload["content"] = self.content
        payload["version_no"] = self.version_no
        payload["access"] = self.access
        payload["lease"] = self.lease.to_dict()
        return payload


class OfficeDocEditorService:
    def __init__(
        self,
        node_repository,
        document_repository,
        version_repository,
        document_service,
        access_resolver,
        edit_lease_service,
        ai_service,
    ) -> None:
        self._node_repository = node_repository
        self._document_repository = document_repository
        self._version_repository = version_repository
        self._document_service = document_service
        self._access_resolver = access_resolver
        self._edit_lease_service = edit_lease_service
        self._ai_service = ai_service

    # ------------------------------------------------------------------
    # Create / open / save
    # ------------------------------------------------------------------

    def create_document(self, owner_user_id, name: str, parent_id=None) -> DocView:
        """Create a node + document of ``doc_type='text'`` with an empty
        content model as version 1 — reuses ``OfficeDocumentService.
        upload_document`` (DRY: node/document/version creation, quota, and
        mime sniffing are not re-implemented here)."""
        node, document, version = self._document_service.upload_document(
            owner_user_id,
            name,
            parent_id,
            empty_content_bytes(),
            doc_type=DOC_TYPE_TEXT,
        )
        content = parse_content_bytes(empty_content_bytes())
        lease = self._edit_lease_service.current(node.id, owner_user_id)
        return DocView(
            node=node,
            document=document,
            content=content,
            version_no=version.version_no,
            access=ACCESS_OWNER,
            lease=lease,
        )

    def open_document(self, user_id, node_id) -> DocView:
        node, document, access = self._resolve_text_document(user_id, node_id)
        _n, _d, version, content_bytes = self._document_service.get_content_for_node(
            node, document
        )
        content = parse_content_bytes(content_bytes)
        lease = self._edit_lease_service.current(node_id, user_id)
        return DocView(
            node=node,
            document=document,
            content=content,
            version_no=version.version_no,
            access=access,
            lease=lease,
        )

    def save_document(
        self,
        user_id,
        node_id,
        content_model: Dict[str, Any],
        base_version_no: int,
        *,
        ai_enabled: Optional[bool] = None,
    ):
        node, document, access = self._resolve_text_document(user_id, node_id)
        if access not in EDIT_CAPABLE_ACCESS:
            raise OfficeShareForbiddenError(node_id)

        self._edit_lease_service.assert_not_locked_by_other(node_id, user_id)
        validate_content_model(content_model)

        current_version_no = self._current_version_no(document)
        if current_version_no != base_version_no:
            raise OfficeDocStaleVersionError(current_version_no)

        if ai_enabled is not None and access == ACCESS_OWNER:
            document.ai_enabled = bool(ai_enabled)
            self._document_repository.add(document)

        data = serialize_content_model(content_model)
        version = self._document_service.add_version_for_node(
            node, document, data, actor_user_id=user_id
        )
        # A successful save is also a successful heartbeat — the saving
        # session keeps (or reclaims, if it just took over) the lease.
        self._edit_lease_service.acquire(node_id, user_id)
        return version

    # ------------------------------------------------------------------
    # Lease / presence
    # ------------------------------------------------------------------

    def acquire_lease(self, user_id, node_id) -> LeaseState:
        _node, _document, access = self._resolve_lockable_document(user_id, node_id)
        if access not in EDIT_CAPABLE_ACCESS:
            raise OfficeShareForbiddenError(node_id)
        return self._edit_lease_service.acquire(node_id, user_id)

    def release_lease(self, user_id, node_id) -> None:
        self._resolve_lockable_document(user_id, node_id)
        self._edit_lease_service.release(node_id, user_id)

    def presence(self, user_id, node_id) -> LeaseState:
        self._resolve_lockable_document(user_id, node_id)
        return self._edit_lease_service.current(node_id, user_id)

    # ------------------------------------------------------------------
    # AI helper
    # ------------------------------------------------------------------

    def run_ai_capability(
        self,
        user_id,
        node_id,
        capability_id: str,
        *,
        selection_text: str = "",
        context_before: str = "",
        context_after: str = "",
        target_language: Optional[str] = None,
    ) -> dict:
        _node, document, access = self._resolve_text_document(user_id, node_id)
        if access not in EDIT_CAPABLE_ACCESS:
            raise OfficeShareForbiddenError(node_id)
        if not document.ai_enabled:
            raise OfficeAiDisabledError(node_id)

        proposed_text, connection_slug = self._ai_service.run_capability(
            user_id,
            node_id,
            capability_id,
            selection_text=selection_text,
            context_before=context_before,
            context_after=context_after,
            target_language=target_language,
        )
        return {
            "capability": capability_id,
            "connection_slug": connection_slug,
            "proposed_text": proposed_text,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_text_document(self, user_id, node_id):
        return resolve_editable_document(
            self._access_resolver,
            self._node_repository,
            self._document_repository,
            user_id,
            node_id,
            allowed_doc_types=(DOC_TYPE_TEXT,),
        )

    def _resolve_lockable_document(self, user_id, node_id):
        """Widened resolution for the content-agnostic lease methods —
        accepts a Doc OR a Sheet node (see the module docstring)."""
        return resolve_editable_document(
            self._access_resolver,
            self._node_repository,
            self._document_repository,
            user_id,
            node_id,
            allowed_doc_types=LOCKABLE_DOC_TYPES,
        )

    def _current_version_no(self, document: OfficeDocument) -> int:
        return current_version_no(document, self._version_repository)
