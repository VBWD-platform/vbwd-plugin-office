"""Reference translation on copy/paste and on row/column insert/delete —
producing ``#REF!`` when the translated target no longer exists."""
from plugins.office.office.sheet.engine import (
    translate_formula_for_column_delete,
    translate_formula_for_column_insert,
    translate_formula_for_copy,
    translate_formula_for_row_delete,
    translate_formula_for_row_insert,
)
from plugins.office.office.sheet.values import ErrorCode

SHEET = "Sheet1"


def test_copy_shifts_relative_reference():
    assert translate_formula_for_copy("=A1+1", row_delta=1, column_delta=0) == "(A2+1)"


def test_copy_keeps_absolute_reference_fixed():
    assert (
        translate_formula_for_copy("=$A$1+1", row_delta=5, column_delta=5) == "($A$1+1)"
    )


def test_copy_shifts_only_the_relative_axis_of_a_mixed_reference():
    # $A1 is column-absolute, row-relative.
    assert translate_formula_for_copy("=$A1", row_delta=2, column_delta=3) == "$A3"


def test_copy_off_the_top_of_the_sheet_becomes_ref_error():
    result = translate_formula_for_copy("=A1", row_delta=-1, column_delta=0)
    assert result == ErrorCode.REF.value


def test_row_insert_shifts_a_reference_below_the_insertion_point():
    formula = translate_formula_for_row_insert("=A5", SHEET, at_row=3, count=2)
    assert formula == "A7"


def test_row_insert_does_not_shift_a_reference_above_the_insertion_point():
    formula = translate_formula_for_row_insert("=A2", SHEET, at_row=3, count=2)
    assert formula == "A2"


def test_row_delete_shifts_a_reference_below_the_deleted_rows():
    formula = translate_formula_for_row_delete("=A10", SHEET, at_row=3, count=2)
    assert formula == "A8"


def test_row_delete_of_the_referenced_row_becomes_ref_error():
    formula = translate_formula_for_row_delete("=A3+1", SHEET, at_row=3, count=2)
    assert formula == f"({ErrorCode.REF.value}+1)"


def test_column_insert_shifts_a_reference_right_of_the_insertion_point():
    formula = translate_formula_for_column_insert("=C1", SHEET, at_column=2, count=1)
    assert formula == "D1"


def test_column_delete_of_the_referenced_column_becomes_ref_error():
    formula = translate_formula_for_column_delete("=B1", SHEET, at_column=2, count=1)
    assert formula == ErrorCode.REF.value


def test_range_translation_invalidates_when_either_corner_is_deleted():
    formula = translate_formula_for_row_delete("=SUM(A1:A5)", SHEET, at_row=1, count=1)
    assert formula == f"SUM({ErrorCode.REF.value})"
