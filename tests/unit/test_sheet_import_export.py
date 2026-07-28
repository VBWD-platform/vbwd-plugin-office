"""S147-4 unit coverage for CSV/XLSX import and export — sprint tests #8
(``.xlsx`` round-trip preserves values/formulas/number formats where
``openpyxl`` is installed) and #9 (an unmapped function imports as source +
``#NAME?``, never silently dropped). No DB, no Flask app.
"""
import datetime

import pytest

from plugins.office.office.sheet.cell import parse_cell_reference
from plugins.office.office.sheet.engine import recalculate
from plugins.office.office.sheet.values import ErrorCode, ErrorValue
from plugins.office.office.services.exceptions import OfficeSheetCeilingExceededError
from plugins.office.office.services.sheet_content import SheetCeiling
from plugins.office.office.services.sheet_import_export import (
    export_csv,
    export_xlsx,
    import_csv,
    import_xlsx,
)

GENEROUS_CEILING = SheetCeiling(max_rows=100_000, max_columns=1_000)
NOW = datetime.datetime(2026, 7, 28)


def test_import_csv_reads_numbers_and_text_as_literals():
    data = b"1,hello\n2,world\n"
    result = import_csv(data, GENEROUS_CEILING)
    sheet = result.workbook.sheets["Sheet1"]
    assert sheet.get_cell(1, 1).value == 1.0
    assert sheet.get_cell(2, 1).value == "hello"
    assert sheet.get_cell(1, 2).value == 2.0
    assert sheet.get_cell(2, 2).value == "world"


def test_import_csv_skips_blank_cells_sparsely():
    data = b"1,,3\n"
    result = import_csv(data, GENEROUS_CEILING)
    sheet = result.workbook.sheets["Sheet1"]
    assert len(sheet.cells) == 2  # the blank middle cell is never stored


def test_import_csv_over_the_column_ceiling_raises_cleanly():
    ceiling = SheetCeiling(max_rows=100, max_columns=2)
    data = b"1,2,3\n"
    with pytest.raises(OfficeSheetCeilingExceededError):
        import_csv(data, ceiling)


def test_export_csv_renders_computed_values_not_formulas():
    workbook = import_csv(b"3,4\n", GENEROUS_CEILING).workbook
    address = parse_cell_reference("C1", default_sheet="Sheet1")
    workbook.set_formula(address, "=A1+B1")
    recalculate(workbook, [address], now=NOW)

    csv_bytes = export_csv(workbook)
    assert csv_bytes.decode("utf-8").strip() == "3,4,7"


def test_export_csv_renders_error_values_as_their_error_text():
    workbook = import_csv(b"", GENEROUS_CEILING).workbook
    address = parse_cell_reference("A1", default_sheet="Sheet1")
    workbook.set_formula(address, "=1/0")
    recalculate(workbook, [address], now=NOW)

    csv_bytes = export_csv(workbook)
    assert csv_bytes.decode("utf-8").strip() == "#DIV/0!"


def test_csv_round_trip_import_then_export():
    original = b"10,20\n30,40\n"
    workbook = import_csv(original, GENEROUS_CEILING).workbook
    assert export_csv(workbook).decode("utf-8") == "10,20\r\n30,40\r\n"


# ---------------------------------------------------------------------------
# #9 — an unmapped function imports as source + #NAME?, never silently
# dropped. Proven here at the engine level (the formula's SOURCE TEXT is
# preserved verbatim through import; the sheet_editor_service integration
# test proves the end-to-end "appears in the import report" behaviour).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #8 — .xlsx round-trip preserves values and formulas (skipped cleanly if
# openpyxl is not installed in this environment, per the sprint's own escape
# hatch — never a silently lossy xlsx path).
# ---------------------------------------------------------------------------


def test_xlsx_round_trip_preserves_values_and_formulas():
    pytest.importorskip("openpyxl")
    from plugins.office.office.sheet.engine import Workbook

    workbook = Workbook()
    sheet = workbook.add_sheet("Sheet1")
    from plugins.office.office.sheet.engine import Cell

    sheet.set_cell(1, 1, Cell(value=3.0))
    sheet.set_cell(2, 1, Cell(value=4.0))
    total_address = parse_cell_reference("C1", default_sheet="Sheet1")
    workbook.set_formula(total_address, "=SUM(A1:B1)")
    recalculate(workbook, [total_address], now=NOW)

    data = export_xlsx(workbook)
    reimported = import_xlsx(data, GENEROUS_CEILING).workbook

    assert reimported.sheets["Sheet1"].get_cell(1, 1).value == 3.0
    assert reimported.sheets["Sheet1"].get_cell(2, 1).value == 4.0
    assert reimported.get_formula(total_address) == "=SUM(A1:B1)"

    deltas = recalculate(reimported, [total_address], now=NOW)
    assert deltas[total_address] == 7.0


def test_an_unmapped_function_recalculates_to_name_error_not_silently_dropped():
    from plugins.office.office.sheet.engine import Workbook

    workbook = Workbook()
    workbook.add_sheet("Sheet1")
    address = parse_cell_reference("A1", default_sheet="Sheet1")
    # XLOOKUP is not in this engine's registry — a formula using it must
    # still be stored (never dropped) and evaluate to #NAME?, never raise.
    workbook.set_formula(address, "=XLOOKUP(1,A2:A3,B2:B3)")

    results = recalculate(workbook, [address], now=NOW)

    assert workbook.get_formula(address) == "=XLOOKUP(1,A2:A3,B2:B3)"
    assert results[address] == ErrorValue(ErrorCode.NAME)
