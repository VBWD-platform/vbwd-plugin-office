"""Sheet content model — a **sparse** JSON workbook, never a dense grid
(S147-4).

A VBWD Spreadsheets document (``office_document.doc_type == 'sheet'``)
stores ``{"sheets": [{"name": ..., "cells": {"A1": {"f": ..., "v": ...}}}],
"active_sheet": ...}`` as its version bytes — the exact same
``office_version`` slot every other document kind uses (S147-1). Sparse
matters: a 10 000-row sheet with 40 filled cells serialises 40 entries, not
one million blanks — a cell with neither a formula nor a non-blank cached
value is simply absent from ``cells`` (see
:func:`serialize_workbook_to_model`).

``v`` is a **cache**, never trusted on load: :func:`build_workbook_from_model`
loads it back into each ``Cell.value`` only as a fallback for cells the
caller does not immediately recalculate (e.g. an unaffected cell mid a
partial recalc), but the engine (``office/sheet/engine.py``) is the sole
authority for a formula cell's value — every editor-service read path
(``open_sheet``, ``recalc``) recalculates every formula cell before
returning, never trusting the stored ``v``. The cache exists so a ``view``
share can render the RAW stored bytes instantly, with no recalc pass, via
the existing generic ``/public/<token>/content`` byte route (S147-2) — that
route does not even parse this module's schema, it just streams the JSON as
opaque content, so an anonymous viewer sees the last-computed values with
zero engine work.

The row/column **ceiling** (S147-4 requirement #4) is enforced here, at the
one place every stored/incoming cell reference is parsed
(:func:`parse_reference_within_ceiling`) — a workbook that would exceed it
raises :class:`OfficeSheetCeilingExceededError` before a single cell is
built, turning an oversized workbook into a clean, cheap ``413`` rather than
an attempt to hold it all in memory.
"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from plugins.office.office.sheet.cell import (
    CellAddress,
    InvalidCellReferenceError,
    format_cell_reference,
    parse_cell_reference,
)
from plugins.office.office.sheet.engine import Cell, Workbook
from plugins.office.office.sheet.values import (
    BLANK,
    Blank,
    CellValue,
    ErrorCode,
    ErrorValue,
)
from plugins.office.office.services.exceptions import (
    OfficeSheetCeilingExceededError,
    OfficeSheetContentInvalidError,
)

DEFAULT_SHEET_NAME = "Sheet1"

#: JSON-safe wrapper keys for the two ``CellValue`` kinds that are not
#: natively JSON types (error / date) — anything else round-trips as a bare
#: JSON scalar (number/string/bool) or is simply absent (blank).
_VALUE_KIND_KEY = "t"
_VALUE_PAYLOAD_KEY = "v"
_VALUE_KIND_ERROR = "error"
_VALUE_KIND_DATE = "date"


@dataclass(frozen=True)
class SheetCeiling:
    """The configured row/column ceiling (S147-4 requirement #4)."""

    max_rows: int
    max_columns: int


def empty_workbook_model() -> Dict[str, Any]:
    """The initial model for a freshly created Sheet: one empty sheet."""
    return {
        "sheets": [{"name": DEFAULT_SHEET_NAME, "cells": {}}],
        "active_sheet": DEFAULT_SHEET_NAME,
    }


def empty_workbook_bytes() -> bytes:
    return serialize_workbook_model(empty_workbook_model())


def serialize_workbook_model(model: Dict[str, Any]) -> bytes:
    return json.dumps(model, separators=(",", ":")).encode("utf-8")


def parse_reference_within_ceiling(
    reference_text: str, default_sheet: str, ceiling: SheetCeiling
) -> CellAddress:
    """Parse an A1 reference and enforce the row/column ceiling in the same
    place — the one gate every incoming or stored cell address passes
    through."""
    try:
        address = parse_cell_reference(reference_text, default_sheet=default_sheet)
    except InvalidCellReferenceError as parse_error:
        raise OfficeSheetContentInvalidError(str(parse_error)) from parse_error
    if address.row > ceiling.max_rows:
        raise OfficeSheetCeilingExceededError(address.row, ceiling.max_rows, "rows")
    if address.column > ceiling.max_columns:
        raise OfficeSheetCeilingExceededError(
            address.column, ceiling.max_columns, "columns"
        )
    return address


def encode_cell_value(value: CellValue) -> Any:
    if isinstance(value, Blank):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return value
    if isinstance(value, ErrorValue):
        return {
            _VALUE_KIND_KEY: _VALUE_KIND_ERROR,
            _VALUE_PAYLOAD_KEY: value.code.value,
        }
    if isinstance(value, (datetime.datetime, datetime.date)):
        return {
            _VALUE_KIND_KEY: _VALUE_KIND_DATE,
            _VALUE_PAYLOAD_KEY: value.isoformat(),
        }
    raise OfficeSheetContentInvalidError(f"Unencodable cell value: {value!r}")


def decode_cell_value(raw: Any) -> CellValue:
    if raw is None:
        return BLANK
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        kind = raw.get(_VALUE_KIND_KEY)
        payload = raw.get(_VALUE_PAYLOAD_KEY)
        if kind == _VALUE_KIND_ERROR:
            try:
                return ErrorValue(ErrorCode(payload))
            except ValueError as error:
                raise OfficeSheetContentInvalidError(
                    f"Unknown cached error code: {payload!r}"
                ) from error
        if kind == _VALUE_KIND_DATE:
            try:
                return datetime.datetime.fromisoformat(str(payload))
            except (TypeError, ValueError) as error:
                raise OfficeSheetContentInvalidError(
                    f"Invalid cached date value: {payload!r}"
                ) from error
    raise OfficeSheetContentInvalidError(f"Unrecognised stored cell value: {raw!r}")


def build_workbook_from_model(model: Dict[str, Any], ceiling: SheetCeiling) -> Workbook:
    """Deserialise a sparse JSON workbook model into the pure engine's
    in-memory :class:`Workbook`. Every address is ceiling-checked. The
    cached ``v`` is loaded into ``Cell.value`` as a fallback only — callers
    that need a trustworthy value MUST recalculate before reading it back."""
    if not isinstance(model, dict) or not isinstance(model.get("sheets"), list):
        raise OfficeSheetContentInvalidError("Workbook model must have a 'sheets' list")

    workbook = Workbook()
    for sheet_model in model["sheets"]:
        if not isinstance(sheet_model, dict):
            raise OfficeSheetContentInvalidError("Every sheet entry must be an object")
        name = sheet_model.get("name")
        if not isinstance(name, str) or not name:
            raise OfficeSheetContentInvalidError("Every sheet needs a non-empty 'name'")
        sheet = workbook.add_sheet(name)
        cells = sheet_model.get("cells") or {}
        if not isinstance(cells, dict):
            raise OfficeSheetContentInvalidError(
                f"Sheet '{name}' cells must be an object"
            )
        for reference_text, cell_model in cells.items():
            if not isinstance(cell_model, dict):
                raise OfficeSheetContentInvalidError(
                    f"Cell '{reference_text}' must be an object"
                )
            address = parse_reference_within_ceiling(reference_text, name, ceiling)
            formula = cell_model.get("f")
            value = decode_cell_value(cell_model.get("v"))
            sheet.set_cell(
                address.column, address.row, Cell(formula=formula, value=value)
            )
    return workbook


def serialize_workbook_to_model(
    workbook: Workbook, active_sheet: Optional[str] = None
) -> Dict[str, Any]:
    """The inverse of :func:`build_workbook_from_model` — SPARSE: a cell
    with neither a formula nor a non-blank cached value is omitted entirely
    (requirement #2/#6: a 10 000-row sheet with 40 filled cells must not
    serialise a million blanks)."""
    sheets_payload = []
    sheet_names = list(workbook.sheets.keys())
    for name in sheet_names:
        sheet = workbook.sheets[name]
        cells_payload: Dict[str, Any] = {}
        for (column, row), cell in sheet.cells.items():
            if cell.formula is None and isinstance(cell.value, Blank):
                continue
            reference = format_cell_reference(
                CellAddress(sheet=None, column=column, row=row), include_sheet=False
            )
            entry: Dict[str, Any] = {}
            if cell.formula is not None:
                entry["f"] = cell.formula
            if not isinstance(cell.value, Blank):
                entry["v"] = encode_cell_value(cell.value)
            cells_payload[reference] = entry
        sheets_payload.append({"name": name, "cells": cells_payload})

    resolved_active_sheet = (
        active_sheet
        if active_sheet in sheet_names
        else (sheet_names[0] if sheet_names else DEFAULT_SHEET_NAME)
    )
    return {"sheets": sheets_payload, "active_sheet": resolved_active_sheet}


def parse_workbook_bytes(data: bytes, ceiling: SheetCeiling) -> Dict[str, Any]:
    """Deserialise a stored version's bytes into the JSON model, validating
    it (shape + ceiling) along the way. Raises
    :class:`OfficeSheetContentInvalidError`/:class:`OfficeSheetCeilingExceededError`."""
    try:
        model = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as parse_error:
        raise OfficeSheetContentInvalidError(
            "Stored content is not valid JSON"
        ) from parse_error
    build_workbook_from_model(model, ceiling)  # validates shape + ceiling
    return model


def coerce_literal_input(raw: Any) -> CellValue:
    """Coerce a JSON value received from the client (a cell's plain
    ``value``, not a formula) into a :class:`CellValue`."""
    if raw is None:
        return BLANK
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        return raw
    raise OfficeSheetContentInvalidError(f"Unsupported literal cell value: {raw!r}")
