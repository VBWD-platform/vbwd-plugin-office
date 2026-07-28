"""Coercion rules (requirement #5): ``"5" + 1``, ``TRUE + 1``, and blank
behaving differently inside ``SUM`` versus ``AVERAGE``."""
from plugins.office.office.sheet.values import (
    BLANK,
    ErrorCode,
    ErrorValue,
    coerce_to_boolean,
    coerce_to_number,
    coerce_to_text,
)

from ._helpers import evaluate_formula


def test_numeric_text_literal_coerces_in_arithmetic():
    assert evaluate_formula('="5"+1') == 6.0


def test_boolean_true_coerces_to_one_in_arithmetic():
    assert evaluate_formula("=TRUE+1") == 2.0


def test_boolean_false_coerces_to_zero_in_arithmetic():
    assert evaluate_formula("=FALSE+1") == 1.0


def test_non_numeric_text_fails_arithmetic_coercion():
    assert evaluate_formula('="abc"+1') == ErrorValue(ErrorCode.VALUE)


def test_blank_coerces_to_zero_in_arithmetic():
    assert coerce_to_number(BLANK) == 0.0


def test_blank_cell_in_sum_contributes_nothing():
    # A3 is never written — SUM(A1:A3) sees it as blank, contributing 0.
    assert evaluate_formula("=SUM(A1:A2)", {"A1": 4, "A2": 6}) == 10.0
    assert evaluate_formula("=SUM(A1:A3)", {"A1": 4, "A2": 6}) == 10.0


def test_blank_cell_in_average_is_excluded_from_the_denominator():
    # AVERAGE(A1:A3) with A3 blank must average over 2 numbers, not 3 — the
    # denominator, not just the numerator, ignores the blank cell.
    assert evaluate_formula("=AVERAGE(A1:A2)", {"A1": 4, "A2": 6}) == 5.0
    assert evaluate_formula("=AVERAGE(A1:A3)", {"A1": 4, "A2": 6}) == 5.0


def test_text_cell_in_sum_range_is_ignored_not_coerced():
    # Unlike a literal "5" argument, a text cell inside a summed RANGE is
    # silently skipped rather than coerced.
    assert evaluate_formula("=SUM(A1:A2)", {"A1": 4, "A2": "not a number"}) == 4.0


def test_boolean_coercion_from_text():
    assert coerce_to_boolean("true") is True
    assert coerce_to_boolean("FALSE") is False
    assert coerce_to_boolean("maybe") == ErrorValue(ErrorCode.VALUE)


def test_text_coercion_of_boolean_and_blank():
    assert coerce_to_text(True) == "TRUE"
    assert coerce_to_text(False) == "FALSE"
    assert coerce_to_text(BLANK) == ""
