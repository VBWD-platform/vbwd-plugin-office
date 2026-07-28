"""Cell addressing: A1 references, ranges, absolute/relative markers, R1C1
conversion, and the pure address-transform rules used for copy/paste and for
row/column insert/delete.

PURE — see ``office/sheet/__init__.py``.

Everything here operates on :class:`CellAddress`/:class:`CellRange` values —
no formula text, no AST, no I/O. ``parser.py`` depends on this module (to
build reference nodes); this module must never depend back on ``parser.py``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterator, Optional

#: A1-style reference, e.g. ``Sheet1!$A$1`` or ``'My Sheet'!B2:C10``.
_CELL_REFERENCE_PATTERN = re.compile(
    r"^(?:(?:'(?P<quoted_sheet>(?:[^']|'')+)'|(?P<bare_sheet>[A-Za-z_][A-Za-z0-9_.]*))!)?"
    r"(?P<column_absolute>\$)?(?P<column>[A-Za-z]{1,3})"
    r"(?P<row_absolute>\$)?(?P<row>[1-9][0-9]*)$"
)

#: R1C1-style reference, e.g. ``R[2]C[-1]``, ``R5C3``, ``RC``.
_R1C1_REFERENCE_PATTERN = re.compile(
    r"^R(?:\[(?P<row_relative>-?\d+)\]|(?P<row_absolute>\d+))?"
    r"C(?:\[(?P<column_relative>-?\d+)\]|(?P<column_absolute>\d+))?$",
    re.IGNORECASE,
)

MAXIMUM_COLUMN_LETTERS = 3


class InvalidCellReferenceError(ValueError):
    """Raised for a malformed A1/R1C1 reference string — a parsing-time
    failure, never surfaced to a formula's evaluated value."""


@dataclass(frozen=True)
class CellAddress:
    """One cell, addressed by 1-based column/row plus its A1 ``$`` markers.

    ``sheet`` is ``None`` for an unqualified reference (``A1``); callers
    resolve it against a default sheet with :func:`resolve_address` before
    using the address as a graph or lookup key, so that ``A1`` and
    ``Sheet1!A1`` compare equal once resolved.
    """

    sheet: Optional[str]
    column: int
    row: int
    column_absolute: bool = False
    row_absolute: bool = False

    def with_sheet(self, sheet_name: str) -> "CellAddress":
        return replace(self, sheet=sheet_name)


@dataclass(frozen=True)
class CellRange:
    """A rectangular range between two corners (order-independent)."""

    start: CellAddress
    end: CellAddress

    def addresses(self) -> Iterator[CellAddress]:
        """Yield every cell in the range, row-major, top-left to bottom-right."""
        sheet = self.start.sheet
        minimum_column, maximum_column = sorted((self.start.column, self.end.column))
        minimum_row, maximum_row = sorted((self.start.row, self.end.row))
        for row in range(minimum_row, maximum_row + 1):
            for column in range(minimum_column, maximum_column + 1):
                yield CellAddress(sheet=sheet, column=column, row=row)


def resolve_address(address: CellAddress, default_sheet: str) -> CellAddress:
    """Fill in ``address.sheet`` from ``default_sheet`` when unqualified."""
    if address.sheet is not None:
        return address
    return replace(address, sheet=default_sheet)


def column_letters_to_index(letters: str) -> int:
    """``"A"`` -> 1, ``"Z"`` -> 26, ``"AA"`` -> 27, …"""
    index = 0
    for character in letters.upper():
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index


def column_index_to_letters(index: int) -> str:
    """The inverse of :func:`column_letters_to_index`."""
    if index < 1:
        raise ValueError(f"column index must be >= 1, got {index}")
    letters = ""
    remaining = index
    while remaining > 0:
        remaining, remainder = divmod(remaining - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def parse_cell_reference(text: str, default_sheet: Optional[str] = None) -> CellAddress:
    """Parse a single A1-style reference such as ``A1``, ``$B$2`` or
    ``'My Sheet'!C3``."""
    match = _CELL_REFERENCE_PATTERN.match(text.strip())
    if not match:
        raise InvalidCellReferenceError(f"not a valid cell reference: {text!r}")
    quoted_sheet = match.group("quoted_sheet")
    if quoted_sheet is not None:
        sheet = quoted_sheet.replace("''", "'")
    else:
        sheet = match.group("bare_sheet") or default_sheet
    column_letters = match.group("column")
    if len(column_letters) > MAXIMUM_COLUMN_LETTERS:
        raise InvalidCellReferenceError(f"column out of range: {text!r}")
    return CellAddress(
        sheet=sheet,
        column=column_letters_to_index(column_letters),
        row=int(match.group("row")),
        column_absolute=match.group("column_absolute") is not None,
        row_absolute=match.group("row_absolute") is not None,
    )


def _quote_sheet_name_if_needed(sheet_name: str) -> str:
    if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", sheet_name):
        return sheet_name
    return "'" + sheet_name.replace("'", "''") + "'"


def format_cell_reference(address: CellAddress, include_sheet: bool = True) -> str:
    """Render a :class:`CellAddress` back to A1 text."""
    column_marker = "$" if address.column_absolute else ""
    row_marker = "$" if address.row_absolute else ""
    reference = (
        f"{column_marker}{column_index_to_letters(address.column)}"
        f"{row_marker}{address.row}"
    )
    if include_sheet and address.sheet is not None:
        return f"{_quote_sheet_name_if_needed(address.sheet)}!{reference}"
    return reference


def format_range_reference(cell_range: CellRange, include_sheet: bool = True) -> str:
    return (
        f"{format_cell_reference(cell_range.start, include_sheet)}:"
        f"{format_cell_reference(cell_range.end, include_sheet=False)}"
    )


def translate_address_for_copy(
    address: CellAddress, row_delta: int, column_delta: int
) -> Optional[CellAddress]:
    """Copy/paste reference translation: relative components shift by the
    paste delta, ``$``-absolute components stay put. Returns ``None`` when
    the translated address would fall off the top/left of the sheet — the
    caller renders that as ``#REF!``."""
    new_row = address.row if address.row_absolute else address.row + row_delta
    new_column = (
        address.column if address.column_absolute else address.column + column_delta
    )
    if new_row < 1 or new_column < 1:
        return None
    return replace(address, row=new_row, column=new_column)


def shift_address_for_row_insert(
    address: CellAddress, at_row: int, count: int
) -> CellAddress:
    """Rows ``[at_row, at_row + count)`` are inserted; every reference at or
    below ``at_row`` moves down by ``count``, absolute or not — the actual
    cell moved, so ``$`` markers are irrelevant here."""
    if address.row >= at_row:
        return replace(address, row=address.row + count)
    return address


def shift_address_for_row_delete(
    address: CellAddress, at_row: int, count: int
) -> Optional[CellAddress]:
    """Rows ``[at_row, at_row + count)`` are deleted. ``None`` means the
    referenced cell was itself deleted — the caller renders ``#REF!``."""
    if at_row <= address.row < at_row + count:
        return None
    if address.row >= at_row + count:
        return replace(address, row=address.row - count)
    return address


def shift_address_for_column_insert(
    address: CellAddress, at_column: int, count: int
) -> CellAddress:
    if address.column >= at_column:
        return replace(address, column=address.column + count)
    return address


def shift_address_for_column_delete(
    address: CellAddress, at_column: int, count: int
) -> Optional[CellAddress]:
    if at_column <= address.column < at_column + count:
        return None
    if address.column >= at_column + count:
        return replace(address, column=address.column - count)
    return address


def _r1c1_axis_part(
    letter: str, is_absolute: bool, absolute_value: int, offset: int
) -> str:
    if is_absolute:
        return f"{letter}{absolute_value}"
    if offset == 0:
        return letter
    return f"{letter}[{offset}]"


def to_r1c1(address: CellAddress, anchor: CellAddress) -> str:
    """Render ``address`` as an R1C1 reference relative to ``anchor``."""
    row_part = _r1c1_axis_part(
        "R", address.row_absolute, address.row, address.row - anchor.row
    )
    column_part = _r1c1_axis_part(
        "C", address.column_absolute, address.column, address.column - anchor.column
    )
    return row_part + column_part


def from_r1c1(text: str, anchor: CellAddress) -> CellAddress:
    """Parse an R1C1 reference relative to ``anchor``."""
    match = _R1C1_REFERENCE_PATTERN.match(text.strip())
    if not match:
        raise InvalidCellReferenceError(f"not a valid R1C1 reference: {text!r}")

    if match.group("row_absolute") is not None:
        row, row_absolute = int(match.group("row_absolute")), True
    else:
        offset = int(match.group("row_relative") or 0)
        row, row_absolute = anchor.row + offset, False

    if match.group("column_absolute") is not None:
        column, column_absolute = int(match.group("column_absolute")), True
    else:
        offset = int(match.group("column_relative") or 0)
        column, column_absolute = anchor.column + offset, False

    return CellAddress(
        sheet=anchor.sheet,
        column=column,
        row=row,
        column_absolute=column_absolute,
        row_absolute=row_absolute,
    )
