"""``OfficeSheetEditorService`` — the S147-4 Spreadsheet orchestrator.

A Sheet is an ``office_node`` of ``kind='document'`` / ``doc_type='sheet'``
(epic D2 — NOT a parallel model): this service delegates every byte-level
read/write to the EXISTING ``OfficeDocumentService`` (node/version/quota
bookkeeping, S147-1/-2) and asks the ONE ``AccessResolver`` (S147-2, epic D5)
for every access decision — the exact same pattern
``OfficeDocEditorService`` (S147-3) established for Docs. This class owns
exactly the Sheet-specific concerns the vault does not already have: the
sparse JSON workbook model, the row/column ceiling, dirty-subgraph
recalculation, and import/export.

Edit-lease acquisition/heartbeat/release/presence is deliberately NOT
duplicated here: a Sheet reuses the SAME ``/docs/<node_id>/lease*`` routes as
a Doc (``OfficeDocEditorService``'s lease methods were widened to accept
``doc_type in {text, sheet}`` — see that module's docstring), and ``save_cells``
/``recalc`` below still call ``EditLeaseService.assert_not_locked_by_other``
directly (mirroring ``OfficeDocEditorService.save_document``) so a save
still respects a lease held by someone else — one home per behaviour, no
second lease mechanism (epic D4).

Recalculation is server-authoritative:

* ``open_sheet``/``export_workbook`` recalculate EVERY formula cell before
  returning — the stored cache is never trusted on load (requirement #3);
  the cache is read directly, uninterpreted, only by the generic
  ``/public/<token>/content`` byte route (S147-2), which streams the raw
  stored JSON with no engine pass at all — that IS the "a view share renders
  instantly from cache" behaviour (requirement #10), and it costs no code
  here.
* ``save_cells`` recalculates ONLY the dirty sub-graph reachable from the
  edited cells (``engine.recalculate``'s own ``changed_cells`` contract) and
  returns ONLY the changed values, never the whole sheet (requirement #5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from vbwd.utils.datetime_utils import utcnow

from plugins.office.office.models.office_document import DOC_TYPE_SHEET, OfficeDocument
from plugins.office.office.models.office_node import OfficeNode
from plugins.office.office.services.access_resolver import ACCESS_OWNER
from plugins.office.office.services.edit_lease_service import LeaseState
from plugins.office.office.services.editable_node_resolver import (
    current_version_no,
    resolve_editable_document,
)
from plugins.office.office.services.exceptions import (
    OfficeDocStaleVersionError,
    OfficeSheetContentInvalidError,
    OfficeSheetExportFormatError,
    OfficeSheetImportFormatError,
    OfficeShareForbiddenError,
)
from plugins.office.office.models.office_share import PERMISSION_EDIT
from plugins.office.office.sheet.cell import format_cell_reference
from plugins.office.office.sheet.engine import Cell, Sheet, Workbook, recalculate
from plugins.office.office.sheet.values import ErrorCode, is_error
from plugins.office.office.services.sheet_content import (
    SheetCeiling,
    build_workbook_from_model,
    coerce_literal_input,
    empty_workbook_bytes,
    empty_workbook_model,
    encode_cell_value,
    parse_reference_within_ceiling,
    parse_workbook_bytes,
    serialize_workbook_model,
    serialize_workbook_to_model,
)
from plugins.office.office.services.sheet_import_export import (
    CSV_FORMAT,
    XLSX_FORMAT,
    CSV_MIME_TYPE,
    XLSX_MIME_TYPE,
    ImportReportEntry,
    export_csv,
    export_xlsx,
    import_csv,
    import_xlsx,
)

#: Access levels that may edit cells or trigger a persisted recalc — matches
#: the sprint's "edit to save, view to read" rule.
EDIT_CAPABLE_ACCESS = frozenset({ACCESS_OWNER, PERMISSION_EDIT})

_SHEET_DOC_TYPES = (DOC_TYPE_SHEET,)


@dataclass(frozen=True)
class SheetView:
    """The full payload for ``POST``/``GET .../sheets`` — the recalculated
    workbook, version, access, and lease state in one response."""

    node: OfficeNode
    document: OfficeDocument
    workbook_model: Dict[str, Any]
    version_no: int
    access: str
    lease: LeaseState

    def to_dict(self) -> dict:
        payload = self.node.to_dict()
        payload["document"] = self.document.to_dict()
        payload["workbook"] = self.workbook_model
        payload["version_no"] = self.version_no
        payload["access"] = self.access
        payload["lease"] = self.lease.to_dict()
        return payload


@dataclass(frozen=True)
class SheetSaveResult:
    """The response for ``PUT .../cells`` and ``POST .../recalc`` — ONLY the
    cells that changed, never the whole sheet (requirement #5)."""

    version_no: int
    changes: Dict[str, Any]

    def to_dict(self) -> dict:
        return {"version_no": self.version_no, "changes": self.changes}


@dataclass(frozen=True)
class SheetImportResult:
    version_no: int
    unmapped_formulas: List[ImportReportEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version_no": self.version_no,
            "unmapped_formulas": [entry.to_dict() for entry in self.unmapped_formulas],
        }


class OfficeSheetEditorService:
    def __init__(
        self,
        node_repository,
        document_repository,
        version_repository,
        document_service,
        access_resolver,
        edit_lease_service,
        max_rows: int,
        max_columns: int,
    ) -> None:
        self._node_repository = node_repository
        self._document_repository = document_repository
        self._version_repository = version_repository
        self._document_service = document_service
        self._access_resolver = access_resolver
        self._edit_lease_service = edit_lease_service
        self._ceiling = SheetCeiling(max_rows=max_rows, max_columns=max_columns)

    # ------------------------------------------------------------------
    # Create / open
    # ------------------------------------------------------------------

    def create_sheet(self, owner_user_id, name: str, parent_id=None) -> SheetView:
        """Create a node + document of ``doc_type='sheet'`` with an empty
        single-sheet workbook as version 1 (reuses ``OfficeDocumentService.
        upload_document`` — DRY: node/document/version creation, quota, and
        mime sniffing are not re-implemented here)."""
        node, document, version = self._document_service.upload_document(
            owner_user_id,
            name,
            parent_id,
            empty_workbook_bytes(),
            doc_type=DOC_TYPE_SHEET,
        )
        lease = self._edit_lease_service.current(node.id, owner_user_id)
        return SheetView(
            node=node,
            document=document,
            workbook_model=empty_workbook_model(),
            version_no=version.version_no,
            access=ACCESS_OWNER,
            lease=lease,
        )

    def open_sheet(self, user_id, node_id) -> SheetView:
        """The engine is the authority: EVERY formula cell is recalculated
        before the response is built, regardless of what ``v`` was cached
        as (requirement #3)."""
        node, document, access = self._resolve_sheet_document(user_id, node_id)
        _n, _d, version, content_bytes = self._document_service.get_content_for_node(
            node, document
        )
        model = parse_workbook_bytes(content_bytes, self._ceiling)
        workbook = build_workbook_from_model(model, self._ceiling)
        self._recalculate_every_formula(workbook)

        fresh_model = serialize_workbook_to_model(
            workbook, active_sheet=model.get("active_sheet")
        )
        lease = self._edit_lease_service.current(node_id, user_id)
        return SheetView(
            node=node,
            document=document,
            workbook_model=fresh_model,
            version_no=version.version_no,
            access=access,
            lease=lease,
        )

    # ------------------------------------------------------------------
    # Save (partial recalc) / recalc (full recalc) — both persist a version
    # ------------------------------------------------------------------

    def save_cells(
        self,
        user_id,
        node_id,
        changes: List[Dict[str, Any]],
        base_version_no: int,
    ) -> SheetSaveResult:
        node, document, access = self._resolve_sheet_document(user_id, node_id)
        self._require_edit_capable(node_id, access)
        self._edit_lease_service.assert_not_locked_by_other(node_id, user_id)

        current_version = current_version_no(document, self._version_repository)
        if current_version != base_version_no:
            raise OfficeDocStaleVersionError(current_version)

        workbook, model = self._load_workbook(node, document)
        changed_addresses = [
            self._apply_change(workbook, model.get("active_sheet"), change)
            for change in changes
        ]

        deltas = recalculate(workbook, changed_addresses, utcnow())
        version = self._persist(node, document, workbook, model, user_id)
        self._edit_lease_service.acquire(node_id, user_id)

        return SheetSaveResult(
            version_no=version.version_no, changes=self._encode_deltas(deltas)
        )

    def recalc(self, user_id, node_id) -> SheetSaveResult:
        """Force a full recalculation and PERSIST the refreshed cache (used
        e.g. after an import, or as an explicit operator action) —
        distinct from ``open_sheet``'s in-memory-only recalc-for-display."""
        node, document, access = self._resolve_sheet_document(user_id, node_id)
        self._require_edit_capable(node_id, access)
        self._edit_lease_service.assert_not_locked_by_other(node_id, user_id)

        workbook, model = self._load_workbook(node, document)
        deltas = self._recalculate_every_formula(workbook)
        version = self._persist(node, document, workbook, model, user_id)

        return SheetSaveResult(
            version_no=version.version_no, changes=self._encode_deltas(deltas)
        )

    # ------------------------------------------------------------------
    # Export / import
    # ------------------------------------------------------------------

    def export_workbook(
        self, user_id, node_id, export_format: str
    ) -> Tuple[bytes, str, str]:
        """Any access level that can open the sheet (owner/edit/comment/view)
        may export it — export is a read, not a write."""
        node, document, _access = self._resolve_sheet_document(user_id, node_id)
        workbook, model = self._load_workbook(node, document)
        self._recalculate_every_formula(workbook)  # export reflects fresh values

        if export_format == CSV_FORMAT:
            data = export_csv(workbook, model.get("active_sheet"))
            return data, CSV_MIME_TYPE, f"{node.name}.csv"
        if export_format == XLSX_FORMAT:
            data = export_xlsx(workbook)
            return data, XLSX_MIME_TYPE, f"{node.name}.xlsx"
        raise OfficeSheetExportFormatError(export_format)

    def import_workbook(
        self, user_id, node_id, data: bytes, import_format: str
    ) -> SheetImportResult:
        node, document, access = self._resolve_sheet_document(user_id, node_id)
        self._require_edit_capable(node_id, access)
        self._edit_lease_service.assert_not_locked_by_other(node_id, user_id)

        if import_format == CSV_FORMAT:
            imported = import_csv(data, self._ceiling)
        elif import_format == XLSX_FORMAT:
            imported = import_xlsx(data, self._ceiling)
        else:
            raise OfficeSheetImportFormatError(import_format)

        workbook = imported.workbook
        deltas = self._recalculate_every_formula(workbook)
        unmapped = self._unmapped_formula_report(workbook, deltas)

        model = serialize_workbook_to_model(workbook)
        data_bytes = serialize_workbook_model(model)
        version = self._document_service.add_version_for_node(
            node, document, data_bytes, actor_user_id=user_id
        )
        return SheetImportResult(
            version_no=version.version_no, unmapped_formulas=unmapped
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_sheet_document(self, user_id, node_id):
        return resolve_editable_document(
            self._access_resolver,
            self._node_repository,
            self._document_repository,
            user_id,
            node_id,
            allowed_doc_types=_SHEET_DOC_TYPES,
        )

    @staticmethod
    def _require_edit_capable(node_id, access: str) -> None:
        if access not in EDIT_CAPABLE_ACCESS:
            raise OfficeShareForbiddenError(node_id)

    def _load_workbook(self, node, document) -> Tuple[Workbook, Dict[str, Any]]:
        _n, _d, _version, content_bytes = self._document_service.get_content_for_node(
            node, document
        )
        model = parse_workbook_bytes(content_bytes, self._ceiling)
        workbook = build_workbook_from_model(model, self._ceiling)
        return workbook, model

    def _persist(self, node, document, workbook: Workbook, model, user_id):
        new_model = serialize_workbook_to_model(
            workbook, active_sheet=model.get("active_sheet")
        )
        data = serialize_workbook_model(new_model)
        return self._document_service.add_version_for_node(
            node, document, data, actor_user_id=user_id
        )

    def _recalculate_every_formula(self, workbook: Workbook):
        all_formula_addresses = [
            address for address, _formula in workbook.iter_formula_addresses()
        ]
        return recalculate(workbook, all_formula_addresses, utcnow())

    def _apply_change(self, workbook: Workbook, default_sheet: Optional[str], change):
        if not isinstance(change, dict):
            raise OfficeSheetContentInvalidError("Each change must be an object")
        sheet_name = change.get("sheet") or default_sheet
        if not isinstance(sheet_name, str) or not sheet_name:
            raise OfficeSheetContentInvalidError("Each change needs a 'sheet'")
        reference = change.get("address")
        if not isinstance(reference, str) or not reference:
            raise OfficeSheetContentInvalidError("Each change needs an 'address'")
        address = parse_reference_within_ceiling(reference, sheet_name, self._ceiling)

        sheet = workbook.sheets.setdefault(sheet_name, Sheet(name=sheet_name))
        if change.get("clear"):
            sheet.set_cell(address.column, address.row, Cell())
        elif change.get("formula") is not None:
            sheet.set_cell(
                address.column, address.row, Cell(formula=str(change["formula"]))
            )
        elif "value" in change:
            literal = coerce_literal_input(change["value"])
            sheet.set_cell(address.column, address.row, Cell(value=literal))
        else:
            raise OfficeSheetContentInvalidError(
                "Each change needs 'formula', 'value', or 'clear'"
            )
        return address

    @staticmethod
    def _encode_deltas(deltas) -> Dict[str, Any]:
        return {
            format_cell_reference(address, include_sheet=True): encode_cell_value(value)
            for address, value in deltas.items()
        }

    @staticmethod
    def _unmapped_formula_report(workbook: Workbook, deltas) -> List[ImportReportEntry]:
        return [
            ImportReportEntry(
                sheet=address.sheet,
                address=format_cell_reference(address, include_sheet=False),
                formula=workbook.get_formula(address) or "",
            )
            for address, value in deltas.items()
            if is_error(value) and value.code == ErrorCode.NAME
        ]
