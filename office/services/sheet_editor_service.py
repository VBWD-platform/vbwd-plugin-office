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

import os

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
    OfficeSheetUnavailableFormatError,
    OfficeShareForbiddenError,
)
from plugins.office.office.models.office_share import PERMISSION_EDIT
from plugins.office.office.sheet.cell import format_cell_reference
from plugins.office.office.sheet.engine import (
    Cell,
    Sheet,
    Workbook,
    recalculate,
    translate_formula_for_copy,
)
from plugins.office.office.sheet.values import ErrorCode, is_error

from plugins.office.office.services.sheet_content import (
    SheetCeiling,
    SheetPresentation,
    apply_presentation,
    build_workbook_from_model,
    coerce_literal_input,
    empty_workbook_bytes,
    empty_workbook_model,
    encode_cell_value,
    extract_presentation,
    format_range_within_sheet,
    normalize_cell_style,
    parse_range_within_ceiling,
    parse_reference_within_ceiling,
    parse_workbook_bytes,
    range_contains,
    ranges_overlap,
    serialize_workbook_model,
    serialize_workbook_to_model,
)
from plugins.office.office.services.sheet_import_export import (
    CSV_FORMAT,
    PDF_FORMAT,
    XLSX_FORMAT,
    CSV_MIME_TYPE,
    PDF_MIME_TYPE,
    XLSX_MIME_TYPE,
    ImportReportEntry,
    export_csv,
    export_xlsx,
    render_sheet_html_table,
    import_csv,
    import_xlsx,
)

#: Access levels that may edit cells or trigger a persisted recalc — matches
#: the sprint's "edit to save, view to read" rule.
_PDF_TEMPLATE_NAME = "office_sheet.html"
_PDF_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "templates", "pdf"
)

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
        pdf_service=None,
    ) -> None:
        self._node_repository = node_repository
        self._document_repository = document_repository
        self._version_repository = version_repository
        self._document_service = document_service
        self._access_resolver = access_resolver
        self._edit_lease_service = edit_lease_service
        # Optional on purpose: CSV/XLSX export needs no PDF machinery, so a
        # unit test (or an install without the core pdf_service) can build this
        # service without one and every non-PDF path keeps working. Asking for
        # a PDF without it raises a clear "unavailable", never an AttributeError.
        self._pdf_service = pdf_service
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
        presentation = extract_presentation(model)
        self._recalculate_every_formula(workbook)

        fresh_model = serialize_workbook_to_model(
            workbook, active_sheet=model.get("active_sheet")
        )
        apply_presentation(fresh_model, presentation)
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
        presentation = extract_presentation(model)
        changed_addresses = self._apply_changes(workbook, presentation, model, changes)

        deltas = recalculate(workbook, changed_addresses, utcnow())
        version = self._persist(node, document, workbook, model, user_id, presentation)
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
        if export_format == PDF_FORMAT:
            data = self._render_pdf(node, workbook, model.get("active_sheet"))
            return data, PDF_MIME_TYPE, f"{node.name}.pdf"
        raise OfficeSheetExportFormatError(export_format)

    def _render_pdf(self, node, workbook, active_sheet) -> bytes:
        """Render the active sheet to PDF through the CORE ``pdf_service``.

        Deliberately the same seam VBWD Docs uses (``doc_export._render_pdf``):
        one HTML body dropped into a registered template, rendered by core. A
        second, sheet-specific PDF engine would be a second thing to keep
        working — the values are already computed, all that differs is the
        template.
        """
        if self._pdf_service is None:
            raise OfficeSheetUnavailableFormatError(
                "PDF export is unavailable: no PDF service is configured"
            )
        body_html = render_sheet_html_table(workbook, active_sheet)
        # Self-heal, mirroring doc_export: if on_enable did not run (a bare
        # unit test), register the template path rather than 500ing.
        self._pdf_service.register_plugin_template_path(_PDF_TEMPLATE_DIR)
        return self._pdf_service.render(
            _PDF_TEMPLATE_NAME, {"title": node.name, "body_html": body_html}
        )

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

    def _persist(
        self,
        node,
        document,
        workbook: Workbook,
        model,
        user_id,
        presentation: Optional[SheetPresentation] = None,
    ):
        """Persist ``workbook`` as a new version. ``presentation`` (styles +
        merges) is re-injected into the freshly-rebuilt model so a save that
        does not touch formatting never drops it — defaults to whatever was
        already in ``model`` (e.g. ``recalc``, which never changes
        presentation, relies on this default)."""
        if presentation is None:
            presentation = extract_presentation(model)
        new_model = serialize_workbook_to_model(
            workbook, active_sheet=model.get("active_sheet")
        )
        apply_presentation(new_model, presentation)
        data = serialize_workbook_model(new_model)
        return self._document_service.add_version_for_node(
            node, document, data, actor_user_id=user_id
        )

    def _recalculate_every_formula(self, workbook: Workbook):
        all_formula_addresses = [
            address for address, _formula in workbook.iter_formula_addresses()
        ]
        return recalculate(workbook, all_formula_addresses, utcnow())

    def _apply_changes(
        self,
        workbook: Workbook,
        presentation: SheetPresentation,
        model: Dict[str, Any],
        changes: List[Dict[str, Any]],
    ) -> List:
        """Apply every change in order and return only the addresses whose
        VALUE may need recalculating — a style/merge/unmerge change returns
        ``None`` from :meth:`_apply_change` (the engine has no notion of
        either) and is filtered out here rather than handed to
        ``engine.recalculate`` as a no-op dirty root."""
        default_sheet = model.get("active_sheet")
        addresses = []
        for change in changes:
            address = self._apply_change(workbook, presentation, default_sheet, change)
            if address is not None:
                addresses.append(address)
        return addresses

    def _apply_change(
        self,
        workbook: Workbook,
        presentation: SheetPresentation,
        default_sheet: Optional[str],
        change,
    ):
        if not isinstance(change, dict):
            raise OfficeSheetContentInvalidError("Each change must be an object")
        sheet_name = change.get("sheet") or default_sheet
        if not isinstance(sheet_name, str) or not sheet_name:
            raise OfficeSheetContentInvalidError("Each change needs a 'sheet'")

        if "merge" in change:
            self._apply_merge(presentation, sheet_name, change["merge"])
            return None
        if "unmerge" in change:
            self._apply_unmerge(presentation, sheet_name, change["unmerge"])
            return None

        reference = change.get("address")
        if not isinstance(reference, str) or not reference:
            raise OfficeSheetContentInvalidError("Each change needs an 'address'")
        address = parse_reference_within_ceiling(reference, sheet_name, self._ceiling)
        sheet = workbook.sheets.setdefault(sheet_name, Sheet(name=sheet_name))

        if change.get("clear"):
            sheet.set_cell(address.column, address.row, Cell())
            return address
        if change.get("formula") is not None:
            sheet.set_cell(
                address.column, address.row, Cell(formula=str(change["formula"]))
            )
            return address
        if "value" in change:
            literal = coerce_literal_input(change["value"])
            sheet.set_cell(address.column, address.row, Cell(value=literal))
            return address
        if change.get("fill_from") is not None:
            self._apply_fill(
                presentation, sheet, sheet_name, address, change["fill_from"]
            )
            return address
        if "style" in change:
            self._apply_style(presentation, sheet_name, address, change["style"])
            return None
        raise OfficeSheetContentInvalidError(
            "Each change needs 'formula', 'value', 'clear', 'fill_from', "
            "'style', 'merge', or 'unmerge'"
        )

    def _apply_style(self, presentation, sheet_name: str, address, raw_style) -> None:
        """The fill-handle/toolbar style slot (S147-4 drag/toolbar slice):
        a DISPLAY-only concern kept out of the engine's ``Cell`` — see
        ``sheet_content.py``'s module docstring on the presentation
        side-channel. ``raw_style is None`` clears any style on this cell."""
        reference = format_cell_reference(address, include_sheet=False)
        sheet_styles = presentation.styles.setdefault(sheet_name, {})
        if raw_style is None:
            sheet_styles.pop(reference, None)
        else:
            sheet_styles[reference] = normalize_cell_style(raw_style)

    def _apply_fill(
        self,
        presentation,
        sheet: Sheet,
        sheet_name: str,
        target_address,
        source_reference,
    ) -> None:
        """The fill-handle drag: copy ``source_reference``'s formula/value
        into ``target_address``. A formula is TRANSLATED through the pure
        engine's own ``translate_formula_for_copy`` (relative references
        shift by the row/column delta, ``$``-absolute references do not) —
        never re-derived here. A literal value copies as-is. The source
        cell's style (if any) rides along, matching Excel/Sheets' own
        fill-handle behaviour."""
        source_address = parse_reference_within_ceiling(
            source_reference, sheet_name, self._ceiling
        )
        source_cell = sheet.get_cell(source_address.column, source_address.row)
        if source_cell.formula is not None:
            translated = translate_formula_for_copy(
                source_cell.formula,
                row_delta=target_address.row - source_address.row,
                column_delta=target_address.column - source_address.column,
            )
            stored_formula = (
                translated if translated.startswith("=") else f"={translated}"
            )
            sheet.set_cell(
                target_address.column, target_address.row, Cell(formula=stored_formula)
            )
        else:
            sheet.set_cell(
                target_address.column, target_address.row, Cell(value=source_cell.value)
            )

        sheet_styles = presentation.styles.get(sheet_name)
        if sheet_styles:
            source_reference_key = format_cell_reference(
                source_address, include_sheet=False
            )
            source_style = sheet_styles.get(source_reference_key)
            if source_style:
                target_reference_key = format_cell_reference(
                    target_address, include_sheet=False
                )
                sheet_styles[target_reference_key] = source_style

    def _apply_merge(self, presentation, sheet_name: str, range_text) -> None:
        """A new merge REPLACES any existing merge it overlaps (never
        co-exists with it) — a cell may belong to at most one merged block."""
        if not isinstance(range_text, str) or not range_text:
            raise OfficeSheetContentInvalidError("merge requires a range string")
        new_range = parse_range_within_ceiling(range_text, sheet_name, self._ceiling)
        existing = presentation.merges.get(sheet_name, [])
        remaining = [
            existing_range_text
            for existing_range_text in existing
            if not ranges_overlap(
                parse_range_within_ceiling(
                    existing_range_text, sheet_name, self._ceiling
                ),
                new_range,
            )
        ]
        remaining.append(format_range_within_sheet(new_range))
        presentation.merges[sheet_name] = remaining

    def _apply_unmerge(self, presentation, sheet_name: str, address_text) -> None:
        """Removes any merge covering ``address_text`` — the client sends
        the clicked cell, not necessarily the merge's own top-left corner."""
        if not isinstance(address_text, str) or not address_text:
            raise OfficeSheetContentInvalidError("unmerge requires a cell address")
        target = parse_reference_within_ceiling(address_text, sheet_name, self._ceiling)
        existing = presentation.merges.get(sheet_name, [])
        presentation.merges[sheet_name] = [
            range_text
            for range_text in existing
            if not range_contains(
                parse_range_within_ceiling(range_text, sheet_name, self._ceiling),
                target,
            )
        ]

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
