"""Shared test-only helpers for exercising the pure formula engine.

Not part of ``office/sheet/`` — this is test infrastructure and may import
whatever is convenient (it is exempt from the purity oracle, which scans
only ``office/sheet/`` itself).
"""
import datetime
from typing import Any, Dict, Optional

from plugins.office.office.sheet import engine
from plugins.office.office.sheet.cell import parse_cell_reference
from plugins.office.office.sheet.values import CellValue

SHEET_NAME = "Sheet1"
DEFAULT_NOW = datetime.datetime(2026, 7, 28, 12, 0, 0)
FORMULA_UNDER_TEST_REF = "Z1"


def build_workbook(cells: Dict[str, Any]) -> engine.Workbook:
    """Build a one-sheet workbook from ``{"A1": value_or_formula, ...}``.
    A string starting with ``=`` is stored as a formula; anything else is
    stored as a literal value."""
    workbook = engine.Workbook()
    workbook.add_sheet(SHEET_NAME)
    for reference_text, value in cells.items():
        address = parse_cell_reference(reference_text, default_sheet=SHEET_NAME)
        if isinstance(value, str) and value.startswith("="):
            workbook.set_formula(address, value)
        else:
            workbook.set_literal(address, value)
    return workbook


def evaluate_formula(
    formula: str,
    cells: Optional[Dict[str, Any]] = None,
    now: datetime.datetime = DEFAULT_NOW,
) -> CellValue:
    """Evaluate ``formula`` as the content of a fresh cell, with ``cells``
    pre-populated as supporting data, and return the computed value."""
    workbook = build_workbook(cells or {})
    target_address = parse_cell_reference(
        FORMULA_UNDER_TEST_REF, default_sheet=SHEET_NAME
    )
    workbook.set_formula(target_address, formula)
    results = engine.recalculate(workbook, [target_address], now=now)
    return results[target_address]
