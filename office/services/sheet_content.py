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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from plugins.office.office.sheet.cell import (
    CellAddress,
    CellRange,
    InvalidCellReferenceError,
    format_cell_reference,
    format_range_reference,
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


# ---------------------------------------------------------------------------
# S147-4 (drag/toolbar slice) — cell FORMATTING and MERGED ranges.
#
# Both are pure DISPLAY concerns the engine (``office/sheet/engine.py``) has
# no interest in and must never learn about: a formatted cell's stored value
# stays a real number so the engine keeps computing on numbers, never on
# formatted text, and a merged range never changes which cells the engine
# evaluates (each cell inside a merge is still computed independently — only
# the fe-user grid renders the block as one and hides the non-top-left
# cells). So neither lives on the engine's ``Cell``/``Workbook`` — they are
# addressed per-cell/per-range but kept in a sibling ``styles``/``merges`` map
# per sheet, extracted from the raw JSON model BEFORE it is handed to
# ``build_workbook_from_model`` and re-injected AFTER ``serialize_workbook_
# to_model`` rebuilds the ``cells`` map from scratch on every save — see
# :func:`extract_presentation`/:func:`apply_presentation`, both called from
# ``OfficeSheetEditorService`` around every read/write of the engine
# ``Workbook`` so this state survives every save exactly like a formula does.
# ---------------------------------------------------------------------------

STYLES_KEY = "styles"
MERGES_KEY = "merges"

NUMBER_FORMAT_GENERAL = "general"
NUMBER_FORMAT_NUMBER = "number"
NUMBER_FORMAT_CURRENCY = "currency"
NUMBER_FORMAT_PERCENT = "percent"
NUMBER_FORMAT_DATE = "date"
ALLOWED_NUMBER_FORMATS = frozenset(
    {
        NUMBER_FORMAT_GENERAL,
        NUMBER_FORMAT_NUMBER,
        NUMBER_FORMAT_CURRENCY,
        NUMBER_FORMAT_PERCENT,
        NUMBER_FORMAT_DATE,
    }
)
ALLOWED_TEXT_ALIGNMENTS = frozenset({"left", "center", "right"})
MAXIMUM_STYLE_DECIMAL_PLACES = 10


@dataclass
class SheetPresentation:
    """Per-workbook display state, keyed by sheet name: ``styles`` maps a
    sheet name to ``{cell_reference: style_dict}``; ``merges`` maps a sheet
    name to a list of ``"A1:C1"``-style range strings."""

    styles: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)
    merges: Dict[str, List[str]] = field(default_factory=dict)


def normalize_cell_style(raw: Any) -> Dict[str, Any]:
    """Validate and narrow a client-submitted style object down to the known
    fields — an unrecognised value for a known field is rejected outright
    (never silently coerced) so a malformed toolbar action fails loudly."""
    if not isinstance(raw, dict):
        raise OfficeSheetContentInvalidError("A cell style must be an object")
    style: Dict[str, Any] = {}
    if "format" in raw:
        number_format = raw["format"]
        if number_format not in ALLOWED_NUMBER_FORMATS:
            raise OfficeSheetContentInvalidError(
                f"Unsupported number format: {number_format!r}"
            )
        style["format"] = number_format
    if "decimals" in raw:
        decimals = raw["decimals"]
        is_valid_integer = isinstance(decimals, int) and not isinstance(decimals, bool)
        if not is_valid_integer or not (0 <= decimals <= MAXIMUM_STYLE_DECIMAL_PLACES):
            raise OfficeSheetContentInvalidError(
                f"decimals must be an integer between 0 and {MAXIMUM_STYLE_DECIMAL_PLACES}"
            )
        style["decimals"] = decimals
    for boolean_field in ("bold", "italic"):
        if boolean_field in raw:
            if not isinstance(raw[boolean_field], bool):
                raise OfficeSheetContentInvalidError(
                    f"{boolean_field} must be a boolean"
                )
            style[boolean_field] = raw[boolean_field]
    if "align" in raw:
        alignment = raw["align"]
        if alignment not in ALLOWED_TEXT_ALIGNMENTS:
            raise OfficeSheetContentInvalidError(
                f"Unsupported alignment: {alignment!r}"
            )
        style["align"] = alignment
    return style


def extract_presentation(model: Dict[str, Any]) -> SheetPresentation:
    """Read the ``styles``/``merges`` side-channel out of a raw workbook
    model — tolerant of a model that predates this slice (no such keys)."""
    styles: Dict[str, Dict[str, Any]] = {}
    merges: Dict[str, List[str]] = {}
    for sheet_model in model.get("sheets") or []:
        if not isinstance(sheet_model, dict):
            continue
        name = sheet_model.get("name")
        if not isinstance(name, str):
            continue
        raw_styles = sheet_model.get(STYLES_KEY)
        if isinstance(raw_styles, dict):
            styles[name] = {
                reference: value
                for reference, value in raw_styles.items()
                if isinstance(value, dict)
            }
        raw_merges = sheet_model.get(MERGES_KEY)
        if isinstance(raw_merges, list):
            merges[name] = [entry for entry in raw_merges if isinstance(entry, str)]
    return SheetPresentation(styles=styles, merges=merges)


def apply_presentation(
    model: Dict[str, Any], presentation: SheetPresentation
) -> Dict[str, Any]:
    """The inverse of :func:`extract_presentation` — inject ``presentation``
    back into ``model["sheets"]`` (in place), called after ``serialize_
    workbook_to_model`` has rebuilt ``sheets``/``cells`` from scratch so the
    formatting/merge state a save did not touch is not silently dropped."""
    for sheet_model in model.get("sheets") or []:
        name = sheet_model.get("name")
        sheet_styles = presentation.styles.get(name)
        if sheet_styles:
            sheet_model[STYLES_KEY] = sheet_styles
        sheet_merges = presentation.merges.get(name)
        if sheet_merges:
            sheet_model[MERGES_KEY] = sheet_merges
    return model


def parse_range_within_ceiling(
    range_text: str, default_sheet: str, ceiling: SheetCeiling
) -> CellRange:
    """Parse an ``"A1:C3"`` (or bare ``"A1"``, a one-cell range) range
    string, ceiling-checking both corners through the same gate every other
    reference passes (:func:`parse_reference_within_ceiling`)."""
    corners = range_text.split(":")
    if len(corners) == 1:
        address = parse_reference_within_ceiling(corners[0], default_sheet, ceiling)
        return CellRange(start=address, end=address)
    if len(corners) != 2:
        raise OfficeSheetContentInvalidError(f"Not a valid range: {range_text!r}")
    start = parse_reference_within_ceiling(corners[0], default_sheet, ceiling)
    end = parse_reference_within_ceiling(corners[1], default_sheet, ceiling)
    return CellRange(start=start, end=end)


def _range_bounds(cell_range: CellRange):
    minimum_column, maximum_column = sorted(
        (cell_range.start.column, cell_range.end.column)
    )
    minimum_row, maximum_row = sorted((cell_range.start.row, cell_range.end.row))
    return minimum_column, maximum_column, minimum_row, maximum_row


def ranges_overlap(first: CellRange, second: CellRange) -> bool:
    """Whether two rectangular ranges share at least one cell — used to keep
    a sheet's merges non-overlapping (a new merge replaces any merge it
    overlaps rather than co-existing with it, which would leave a cell
    belonging to two merged blocks at once)."""
    first_min_col, first_max_col, first_min_row, first_max_row = _range_bounds(first)
    second_min_col, second_max_col, second_min_row, second_max_row = _range_bounds(
        second
    )
    columns_overlap = (
        first_min_col <= second_max_col and second_min_col <= first_max_col
    )
    rows_overlap = first_min_row <= second_max_row and second_min_row <= first_max_row
    return columns_overlap and rows_overlap


def range_contains(cell_range: CellRange, address: CellAddress) -> bool:
    minimum_column, maximum_column, minimum_row, maximum_row = _range_bounds(cell_range)
    return (
        minimum_column <= address.column <= maximum_column
        and minimum_row <= address.row <= maximum_row
    )


def format_range_within_sheet(cell_range: CellRange) -> str:
    """Render a range back to ``"A1:C3"`` text, top-left:bottom-right,
    without a sheet qualifier (merges are stored per-sheet already)."""
    minimum_column, maximum_column, minimum_row, maximum_row = _range_bounds(cell_range)
    normalized = CellRange(
        start=CellAddress(sheet=None, column=minimum_column, row=minimum_row),
        end=CellAddress(sheet=None, column=maximum_column, row=maximum_row),
    )
    return format_range_reference(normalized, include_sheet=False)


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
