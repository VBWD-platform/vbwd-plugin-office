"""The spreadsheet value lattice: ``number | text | boolean | date | error | blank``.

PURE — see ``office/sheet/__init__.py`` for the discipline this package is held
to. This module owns the only representation of "what a cell can hold" and the
coercion rules between representations. Every other module in ``sheet/``
imports its notion of a value from here rather than inventing its own — one
home for "what does TRUE + 1 mean" (`feedback_variable_naming` / DRY).

A ``CellValue`` is one of:
  * ``float``                       — number
  * ``str``                         — text
  * ``bool``                        — boolean (checked before ``float`` in all
                                       predicates below, since ``bool`` is a
                                       Python subclass of ``int``)
  * ``datetime.date`` / ``datetime.datetime`` — date / date-time
  * :class:`ErrorValue`             — one of the error codes in the lattice
  * :class:`Blank`                  — the empty-cell value (singleton ``BLANK``)
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import Enum
from typing import TypeGuard, Union

#: Excel's epoch for serial date arithmetic: day 1 = 1900-01-01, but day 0 is
#: kept as 1899-12-30 to reproduce the (deliberately preserved) Lotus 1-2-3
#: leap-year bug that every spreadsheet application still honours today.
EXCEL_EPOCH = datetime.date(1899, 12, 30)


class ErrorCode(str, Enum):
    """The error-value lattice. Every member is a value a formula can
    evaluate to — never an exception a caller has to catch."""

    DIV0 = "#DIV/0!"
    REF = "#REF!"
    VALUE = "#VALUE!"
    NAME = "#NAME?"
    NOT_AVAILABLE = "#N/A"
    NUMBER = "#NUM!"
    CYCLE = "#CYCLE!"


@dataclass(frozen=True)
class ErrorValue:
    """A formula error, carried as data through the value lattice."""

    code: ErrorCode

    def __str__(self) -> str:
        return self.code.value


@dataclass(frozen=True)
class Blank:
    """The value of a cell with no formula and no literal — spreadsheet
    "nothing", distinct from the number zero and the empty string."""


#: The single blank instance — cells that have never been written to.
BLANK = Blank()

CellValue = Union[float, str, bool, datetime.date, datetime.datetime, ErrorValue, Blank]


def is_error(value: CellValue) -> TypeGuard[ErrorValue]:
    return isinstance(value, ErrorValue)


def is_blank(value: CellValue) -> bool:
    return isinstance(value, Blank)


def is_boolean(value: CellValue) -> bool:
    return isinstance(value, bool)


def is_number(value: CellValue) -> bool:
    # bool is an int subclass in Python — exclude it explicitly so a TRUE
    # cell is never mistaken for the number 1 by a caller scanning a range.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_text(value: CellValue) -> bool:
    return isinstance(value, str)


def is_date(value: CellValue) -> bool:
    return isinstance(value, (datetime.date, datetime.datetime))


def first_error(*values: CellValue) -> "ErrorValue | None":
    """Return the first error among ``values``, or ``None`` if there is none.

    Used by function implementations to propagate the earliest error rather
    than repeating the same ``is_error`` chain in every family module (DRY).
    """
    for value in values:
        if is_error(value):
            return value
    return None


def date_to_serial(value: Union[datetime.date, datetime.datetime]) -> float:
    """Convert a date/datetime to an Excel-style serial day number."""
    if isinstance(value, datetime.datetime):
        whole_days = (value.date() - EXCEL_EPOCH).days
        fraction_of_day = (
            value.hour * 3600 + value.minute * 60 + value.second
        ) / 86400.0
        return whole_days + fraction_of_day
    return float((value - EXCEL_EPOCH).days)


def serial_to_date(serial: float) -> datetime.datetime:
    """Convert an Excel-style serial day number back to a ``datetime``."""
    whole_days = int(serial)
    fraction_of_day = serial - whole_days
    base = EXCEL_EPOCH + datetime.timedelta(days=whole_days)
    seconds = round(fraction_of_day * 86400)
    return datetime.datetime.combine(base, datetime.time()) + datetime.timedelta(
        seconds=seconds
    )


def coerce_to_number(value: CellValue) -> Union[float, ErrorValue]:
    """Coerce any lattice value to a number, per the spreadsheet's own rules:
    booleans are 1/0, blank is 0, numeric text parses, non-numeric text and
    unparsable values are ``#VALUE!``."""
    if is_error(value):
        return value
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Blank):
        return 0.0
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return 0.0
        try:
            return float(stripped)
        except ValueError:
            return ErrorValue(ErrorCode.VALUE)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return date_to_serial(value)
    return ErrorValue(ErrorCode.VALUE)


def coerce_to_text(value: CellValue) -> str:
    """Coerce any non-error lattice value to its display text. Callers must
    check ``is_error`` before calling this — an error is a value to
    propagate, never a string to render."""
    if isinstance(value, Blank):
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return repr(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, ErrorValue):
        return str(value)
    return str(value)


def coerce_to_boolean(value: CellValue) -> Union[bool, ErrorValue]:
    """Coerce any lattice value to a boolean, per spreadsheet rules: any
    non-zero number is TRUE, blank is FALSE, and text must spell TRUE/FALSE
    exactly (case-insensitive) or the coercion fails."""
    if is_error(value):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, Blank):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalised = value.strip().upper()
        if normalised == "TRUE":
            return True
        if normalised == "FALSE":
            return False
        return ErrorValue(ErrorCode.VALUE)
    return ErrorValue(ErrorCode.VALUE)


#: Type-ordering rank used by :func:`compare_values` when the two operands
#: are not the same kind — mirrors the common spreadsheet convention
#: ``number < text < boolean``, with blank treated as the number zero.
_BLANK_RANK = 0
_NUMBER_RANK = 1
_TEXT_RANK = 2
_BOOLEAN_RANK = 3


def _comparison_key(value: CellValue):
    if isinstance(value, Blank):
        return (_BLANK_RANK, 0.0)
    if isinstance(value, bool):
        return (_BOOLEAN_RANK, value)
    if isinstance(value, (int, float)):
        return (_NUMBER_RANK, float(value))
    if isinstance(value, (datetime.date, datetime.datetime)):
        return (_NUMBER_RANK, date_to_serial(value))
    if isinstance(value, str):
        return (_TEXT_RANK, value.upper())
    return (_TEXT_RANK, str(value))


def compare_values(left: CellValue, right: CellValue) -> Union[int, ErrorValue]:
    """Three-way compare two lattice values: -1 / 0 / 1, or an error if
    either side is one. Same-type operands compare on their own terms
    (numeric, case-insensitive text, boolean); cross-type operands compare
    by the type rank above — documented behaviour, not full Excel parity
    for exotic mixed-type comparisons (see the sprint's function-coverage
    note on honesty over implied Excel parity)."""
    error = first_error(left, right)
    if error is not None:
        return error
    left_rank, left_key = _comparison_key(left)
    right_rank, right_key = _comparison_key(right)
    if left_rank != right_rank:
        return -1 if left_rank < right_rank else 1
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0
