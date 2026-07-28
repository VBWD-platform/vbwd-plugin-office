"""Shared node/document/access resolution for the office editors that sit on
top of the S147-2 ``AccessResolver`` — VBWD Docs (S147-3) and VBWD
Spreadsheets (S147-4).

Both editors need the exact same shape of answer: "does this node exist, is
it a document of one of my expected ``doc_type``s, and what access level does
this caller have — or is the honest answer a 404 that hides existence from a
caller who is not the owner and holds no share (test #9)?" Before this
module, ``OfficeDocEditorService`` re-implemented this walk privately; the
Sheet editor needing the identical walk (with a different ``doc_type`` set)
is what promoted it to a shared module rather than a second, drifting copy
(DRY) — one home per behaviour, the same principle the sprint applies to the
lease/export routes.

``resolve_editable_document`` is also used for the lease-only operations
(acquire/release/presence), which are content-agnostic: a Doc and a Sheet
share the exact same edit-lease mechanism (epic D4), so those three
``OfficeDocEditorService`` methods resolve with
``allowed_doc_types=(DOC_TYPE_TEXT, DOC_TYPE_SHEET)`` rather than the
content-specific single type.
"""
from __future__ import annotations

from typing import Iterable, Tuple

from plugins.office.office.models.office_node import NODE_KIND_DOCUMENT
from plugins.office.office.services.exceptions import OfficeNodeNotFoundError


def resolve_editable_document(
    access_resolver,
    node_repository,
    document_repository,
    user_id,
    node_id,
    allowed_doc_types: Iterable[str],
) -> Tuple[object, object, str]:
    """Return ``(node, document, access)`` or raise
    :class:`OfficeNodeNotFoundError` — never discloses more than that a node
    is unreachable, whatever the underlying reason (missing, wrong kind,
    wrong ``doc_type``, or no access at all)."""
    access = access_resolver.resolve(node_id, user_id=user_id)
    if access is None:
        raise OfficeNodeNotFoundError(node_id)
    node = node_repository.find_by_id(node_id)
    if node is None or node.kind != NODE_KIND_DOCUMENT:
        raise OfficeNodeNotFoundError(node_id)
    document = document_repository.find_by_node_id(node.id)
    if document is None or document.doc_type not in allowed_doc_types:
        raise OfficeNodeNotFoundError(node_id)
    return node, document, access


def current_version_no(document, version_repository) -> int:
    """The version number a save's ``base_version_no`` must match — ``0``
    for a document with no version yet (never trusted from the client)."""
    if document.current_version_id is None:
        return 0
    version = version_repository.find_by_id(document.current_version_id)
    return version.version_no if version is not None else 0
