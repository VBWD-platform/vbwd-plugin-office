"""Table recognition from an uploaded ``.csv``/``.xlsx`` for the VBWD Docs
"insert table from file" toolbar action (S147-3 toolbar bundle).

Reuses ``sheet_import_export.py``'s parser VERBATIM (openpyxl under the
hood) — the sprint is explicit that a second parser must never be written
(DRY). This module only (a) enforces the Doc-specific access bar (the SAME
``resolve_editable_document`` walk every other Doc write route uses) and
(b) turns the resulting ``Workbook`` into the flat rows-of-strings shape a
Tiptap ``table`` node needs, recalculating every formula cell first — a Doc
table cell is plain text, never a formula (``doc_content.py``'s schema has
no formula concept).

Large sheets are capped, not gracefully truncated: an oversized upload
raises the SAME ``OfficeSheetCeilingExceededError`` the Spreadsheets import
route already maps to a clean ``413`` (reused, not re-invented) — a clear
message rather than an attempt to hold an unbounded table in memory or
freeze the browser rendering it.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import List

from plugins.office.office.models.office_document import DOC_TYPE_TEXT
from plugins.office.office.services.doc_editor_service import EDIT_CAPABLE_ACCESS
from plugins.office.office.services.editable_node_resolver import (
    resolve_editable_document,
)
from plugins.office.office.services.exceptions import (
    OfficeDocTableImportFormatError,
    OfficeShareForbiddenError,
)
from plugins.office.office.services.sheet_content import SheetCeiling
from plugins.office.office.services.sheet_import_export import (
    CSV_FORMAT,
    XLSX_FORMAT,
    import_csv,
    import_xlsx,
)
from plugins.office.office.sheet.engine import recalculate
from plugins.office.office.sheet.values import coerce_to_text

SUPPORTED_TABLE_IMPORT_FORMATS = (CSV_FORMAT, XLSX_FORMAT)


@dataclass(frozen=True)
class ImportedTable:
    """The recognised table, as plain display text — a Doc table cell has
    no formula concept, so every formula cell has already been reduced to
    its computed value by the time this is built."""

    rows: List[List[str]]

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "row_count": len(self.rows),
            "column_count": max((len(row) for row in self.rows), default=0),
        }


def parse_table_from_upload(
    data: bytes, import_format: str, *, max_rows: int, max_columns: int
) -> ImportedTable:
    """Pure parsing (no DB, no access check) — unit-testable on its own;
    :class:`OfficeDocTableImportService` below adds the access-checked,
    DI-composed entry point the route calls."""
    if import_format not in SUPPORTED_TABLE_IMPORT_FORMATS:
        raise OfficeDocTableImportFormatError(
            f"Unsupported table import format: {import_format!r}"
        )

    ceiling = SheetCeiling(max_rows=max_rows, max_columns=max_columns)
    imported = (
        import_csv(data, ceiling)
        if import_format == CSV_FORMAT
        else import_xlsx(data, ceiling)
    )
    workbook = imported.workbook
    sheet_name = next(iter(workbook.sheets), None)
    if sheet_name is None:
        return ImportedTable(rows=[])
    sheet = workbook.sheets[sheet_name]

    formula_addresses = [
        address for address, _formula in workbook.iter_formula_addresses()
    ]
    if formula_addresses:
        recalculate(workbook, formula_addresses, datetime.datetime.utcnow())

    if not sheet.cells:
        return ImportedTable(rows=[])
    max_row = max(row for _column, row in sheet.cells)
    max_column = max(column for column, _row in sheet.cells)
    rows = [
        [
            coerce_to_text(sheet.get_cell(column, row).value)
            for column in range(1, max_column + 1)
        ]
        for row in range(1, max_row + 1)
    ]
    return ImportedTable(rows=rows)


class OfficeDocTableImportService:
    """Access-checked wrapper around :func:`parse_table_from_upload` —
    mirrors how ``OfficeSheetEditorService`` wraps ``sheet_import_export``
    for the Spreadsheets side (an existing, established pattern this class
    follows rather than re-derives)."""

    def __init__(
        self,
        node_repository,
        document_repository,
        access_resolver,
        *,
        max_rows: int,
        max_columns: int,
    ) -> None:
        self._node_repository = node_repository
        self._document_repository = document_repository
        self._access_resolver = access_resolver
        self._max_rows = max_rows
        self._max_columns = max_columns

    def import_table(
        self, user_id, node_id, data: bytes, import_format: str
    ) -> ImportedTable:
        _node, _document, access = resolve_editable_document(
            self._access_resolver,
            self._node_repository,
            self._document_repository,
            user_id,
            node_id,
            allowed_doc_types=(DOC_TYPE_TEXT,),
        )
        if access not in EDIT_CAPABLE_ACCESS:
            raise OfficeShareForbiddenError(node_id)
        return parse_table_from_upload(
            data,
            import_format,
            max_rows=self._max_rows,
            max_columns=self._max_columns,
        )
