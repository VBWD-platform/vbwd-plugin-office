"""S147-4 unit coverage for the sparse JSON workbook model — sprint tests
#6 (sparse serialisation) and #7 (over-ceiling -> a clean error, no OOM).
No DB, no Flask app.
"""
import datetime

import pytest

from plugins.office.office.sheet.engine import Cell, recalculate
from plugins.office.office.sheet.cell import parse_cell_reference
from plugins.office.office.sheet.values import ErrorCode, ErrorValue
from plugins.office.office.services.exceptions import (
    OfficeSheetCeilingExceededError,
    OfficeSheetContentInvalidError,
)
from plugins.office.office.services.sheet_content import (
    SheetCeiling,
    SheetPresentation,
    apply_presentation,
    build_workbook_from_model,
    coerce_literal_input,
    empty_workbook_bytes,
    empty_workbook_model,
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

GENEROUS_CEILING = SheetCeiling(max_rows=100_000, max_columns=1_000)


def test_empty_workbook_model_has_one_empty_sheet():
    model = empty_workbook_model()
    assert model["sheets"] == [{"name": "Sheet1", "cells": {}}]
    assert model["active_sheet"] == "Sheet1"


def test_empty_workbook_bytes_round_trips_through_parse():
    model = parse_workbook_bytes(empty_workbook_bytes(), GENEROUS_CEILING)
    assert model == empty_workbook_model()


def test_build_then_serialize_round_trips_a_formula_and_a_literal():
    model = {
        "sheets": [
            {
                "name": "Sheet1",
                "cells": {
                    "A1": {"v": 5.0},
                    "B1": {"f": "=A1*2", "v": 10.0},
                },
            }
        ],
        "active_sheet": "Sheet1",
    }
    workbook = build_workbook_from_model(model, GENEROUS_CEILING)
    round_tripped = serialize_workbook_to_model(workbook, active_sheet="Sheet1")
    assert round_tripped == model


# ---------------------------------------------------------------------------
# #6 — sparse serialisation: a 10 000-row sheet with 40 filled cells stays
# small, not a million blanks.
# ---------------------------------------------------------------------------


def test_a_large_sparse_sheet_serialises_only_the_filled_cells():
    ceiling = SheetCeiling(max_rows=10_000, max_columns=10)
    model = {
        "sheets": [
            {
                "name": "Sheet1",
                "cells": {
                    f"A{row}": {"v": float(row)}
                    for row in range(1, 41)  # 40 filled cells out of 10,000 rows
                },
            }
        ],
        "active_sheet": "Sheet1",
    }
    workbook = build_workbook_from_model(model, ceiling)
    serialised = serialize_workbook_model(serialize_workbook_to_model(workbook))
    assert len(serialised) < 5_000  # nowhere near a million-blank-cell payload
    assert len(workbook.sheets["Sheet1"].cells) == 40


def test_a_blank_cell_with_neither_formula_nor_value_is_never_serialised():
    ceiling = SheetCeiling(max_rows=100, max_columns=10)
    workbook = build_workbook_from_model(empty_workbook_model(), ceiling)
    workbook.sheets["Sheet1"].set_cell(1, 1, Cell())  # explicitly blank

    model = serialize_workbook_to_model(workbook)
    assert model["sheets"][0]["cells"] == {}


# ---------------------------------------------------------------------------
# #7 — over-ceiling workbook -> a clean error, never an OOM.
# ---------------------------------------------------------------------------


def test_a_reference_beyond_the_row_ceiling_raises_cleanly():
    ceiling = SheetCeiling(max_rows=100, max_columns=10)
    with pytest.raises(OfficeSheetCeilingExceededError):
        parse_reference_within_ceiling("A101", "Sheet1", ceiling)


def test_a_reference_beyond_the_column_ceiling_raises_cleanly():
    ceiling = SheetCeiling(max_rows=100, max_columns=10)
    with pytest.raises(OfficeSheetCeilingExceededError):
        parse_reference_within_ceiling("K1", "Sheet1", ceiling)  # column 11


def test_a_stored_workbook_beyond_the_ceiling_is_rejected_on_load():
    ceiling = SheetCeiling(max_rows=100, max_columns=10)
    model = {
        "sheets": [{"name": "Sheet1", "cells": {"A101": {"v": 1.0}}}],
        "active_sheet": "Sheet1",
    }
    with pytest.raises(OfficeSheetCeilingExceededError):
        build_workbook_from_model(model, ceiling)


# ---------------------------------------------------------------------------
# Value encoding — errors and dates round-trip through the JSON-safe wrapper.
# ---------------------------------------------------------------------------


def test_an_error_value_round_trips():
    model = {
        "sheets": [{"name": "Sheet1", "cells": {"A1": {"f": "=1/0"}}}],
        "active_sheet": "Sheet1",
    }
    workbook = build_workbook_from_model(model, GENEROUS_CEILING)
    address = parse_cell_reference("A1", default_sheet="Sheet1")
    recalculate(workbook, [address], now=datetime.datetime(2026, 7, 28))

    round_tripped = serialize_workbook_to_model(workbook)
    assert round_tripped["sheets"][0]["cells"]["A1"]["v"] == {
        "t": "error",
        "v": "#DIV/0!",
    }

    reloaded = build_workbook_from_model(round_tripped, GENEROUS_CEILING)
    assert reloaded.get_cached_value(address) == ErrorValue(ErrorCode.DIV0)


def test_a_date_value_round_trips():
    model = {
        "sheets": [
            {
                "name": "Sheet1",
                "cells": {"A1": {"v": {"t": "date", "v": "2026-07-28T00:00:00"}}},
            }
        ],
        "active_sheet": "Sheet1",
    }
    workbook = build_workbook_from_model(model, GENEROUS_CEILING)
    address = parse_cell_reference("A1", default_sheet="Sheet1")
    assert workbook.get_cached_value(address) == datetime.datetime(2026, 7, 28)


def test_an_unrecognised_cached_value_is_rejected():
    model = {
        "sheets": [{"name": "Sheet1", "cells": {"A1": {"v": {"t": "mystery"}}}}],
        "active_sheet": "Sheet1",
    }
    with pytest.raises(OfficeSheetContentInvalidError):
        build_workbook_from_model(model, GENEROUS_CEILING)


def test_a_missing_sheets_list_is_rejected():
    with pytest.raises(OfficeSheetContentInvalidError):
        build_workbook_from_model({"not_sheets": []}, GENEROUS_CEILING)


def test_coerce_literal_input_supports_number_text_boolean_and_none():
    assert coerce_literal_input(5) == 5.0
    assert coerce_literal_input("hello") == "hello"
    assert coerce_literal_input(True) is True
    from plugins.office.office.sheet.values import BLANK

    assert coerce_literal_input(None) == BLANK


def test_coerce_literal_input_rejects_an_unsupported_type():
    with pytest.raises(OfficeSheetContentInvalidError):
        coerce_literal_input(["not", "a", "scalar"])


# ---------------------------------------------------------------------------
# S147-4 (drag/toolbar slice) — the styles/merges presentation side-channel.
# ---------------------------------------------------------------------------


def test_extract_presentation_reads_styles_and_merges_from_a_raw_model():
    model = {
        "sheets": [
            {
                "name": "Sheet1",
                "cells": {},
                "styles": {"A1": {"bold": True}},
                "merges": ["A1:C1"],
            }
        ],
        "active_sheet": "Sheet1",
    }
    presentation = extract_presentation(model)
    assert presentation.styles == {"Sheet1": {"A1": {"bold": True}}}
    assert presentation.merges == {"Sheet1": ["A1:C1"]}


def test_extract_presentation_tolerates_a_model_with_no_presentation_keys():
    presentation = extract_presentation(empty_workbook_model())
    assert presentation.styles == {}
    assert presentation.merges == {}


def test_apply_presentation_injects_styles_and_merges_into_a_fresh_model():
    model = {"sheets": [{"name": "Sheet1", "cells": {}}], "active_sheet": "Sheet1"}
    presentation = SheetPresentation(
        styles={"Sheet1": {"A1": {"bold": True}}}, merges={"Sheet1": ["A1:B1"]}
    )
    result = apply_presentation(model, presentation)
    assert result["sheets"][0]["styles"] == {"A1": {"bold": True}}
    assert result["sheets"][0]["merges"] == ["A1:B1"]


def test_apply_presentation_is_a_noop_when_nothing_is_tracked_for_a_sheet():
    model = {"sheets": [{"name": "Sheet1", "cells": {}}], "active_sheet": "Sheet1"}
    apply_presentation(model, SheetPresentation())
    assert "styles" not in model["sheets"][0]
    assert "merges" not in model["sheets"][0]


def test_normalize_cell_style_accepts_known_fields():
    style = normalize_cell_style(
        {
            "format": "currency",
            "decimals": 2,
            "bold": True,
            "italic": False,
            "align": "right",
        }
    )
    assert style == {
        "format": "currency",
        "decimals": 2,
        "bold": True,
        "italic": False,
        "align": "right",
    }


def test_normalize_cell_style_rejects_an_unknown_number_format():
    with pytest.raises(OfficeSheetContentInvalidError):
        normalize_cell_style({"format": "scientific"})


def test_normalize_cell_style_rejects_a_non_boolean_bold():
    with pytest.raises(OfficeSheetContentInvalidError):
        normalize_cell_style({"bold": "yes"})


def test_normalize_cell_style_rejects_an_out_of_range_decimals():
    with pytest.raises(OfficeSheetContentInvalidError):
        normalize_cell_style({"decimals": 99})


def test_normalize_cell_style_rejects_a_non_object():
    with pytest.raises(OfficeSheetContentInvalidError):
        normalize_cell_style("bold")


def test_parse_range_within_ceiling_parses_two_corners():
    cell_range = parse_range_within_ceiling("A1:C3", "Sheet1", GENEROUS_CEILING)
    assert cell_range.start == parse_cell_reference("A1", default_sheet="Sheet1")
    assert cell_range.end == parse_cell_reference("C3", default_sheet="Sheet1")


def test_parse_range_within_ceiling_accepts_a_bare_single_cell():
    cell_range = parse_range_within_ceiling("B2", "Sheet1", GENEROUS_CEILING)
    assert (
        cell_range.start
        == cell_range.end
        == parse_cell_reference("B2", default_sheet="Sheet1")
    )


def test_parse_range_within_ceiling_rejects_a_malformed_range():
    with pytest.raises(OfficeSheetContentInvalidError):
        parse_range_within_ceiling("A1:B2:C3", "Sheet1", GENEROUS_CEILING)


def test_ranges_overlap_detects_an_overlapping_pair():
    first = parse_range_within_ceiling("A1:C3", "Sheet1", GENEROUS_CEILING)
    second = parse_range_within_ceiling("C3:D4", "Sheet1", GENEROUS_CEILING)
    assert ranges_overlap(first, second) is True


def test_ranges_overlap_is_false_for_disjoint_ranges():
    first = parse_range_within_ceiling("A1:B2", "Sheet1", GENEROUS_CEILING)
    second = parse_range_within_ceiling("D4:E5", "Sheet1", GENEROUS_CEILING)
    assert ranges_overlap(first, second) is False


def test_range_contains_a_cell_inside_the_rectangle():
    cell_range = parse_range_within_ceiling("A1:C3", "Sheet1", GENEROUS_CEILING)
    assert (
        range_contains(cell_range, parse_cell_reference("B2", default_sheet="Sheet1"))
        is True
    )
    assert (
        range_contains(cell_range, parse_cell_reference("D4", default_sheet="Sheet1"))
        is False
    )


def test_format_range_within_sheet_normalizes_corner_order():
    cell_range = parse_range_within_ceiling("C3:A1", "Sheet1", GENEROUS_CEILING)
    assert format_range_within_sheet(cell_range) == "A1:C3"
