"""The golden-file engine suite — the spine of S147-4 (written first, per the
sprint's TDD ordering): a table of ``formula -> expected value`` covering
every registered function and operator, including their error cases.

Every entry is ``(case_id, formula, supporting_cells, expected_value)``.
``supporting_cells`` pre-populates cells the formula reads via reference.
"""
import datetime

import pytest

from plugins.office.office.sheet.values import ErrorCode, ErrorValue

from ._helpers import DEFAULT_NOW, evaluate_formula

GOLDEN_CASES = [
    # ---- arithmetic operators -------------------------------------------------
    ("add", "=1+2", {}, 3.0),
    ("subtract", "=10-4", {}, 6.0),
    ("multiply", "=3*4", {}, 12.0),
    ("divide", "=10/2", {}, 5.0),
    ("divide_by_zero", "=10/0", {}, ErrorValue(ErrorCode.DIV0)),
    ("power", "=2^10", {}, 1024.0),
    ("unary_minus", "=-5", {}, -5.0),
    ("unary_plus", "=+5", {}, 5.0),
    ("operator_precedence", "=2+3*4", {}, 14.0),
    ("parentheses_override_precedence", "=(2+3)*4", {}, 20.0),
    # ---- comparison operators ---------------------------------------------
    ("less_than_true", "=1<2", {}, True),
    ("less_than_false", "=2<1", {}, False),
    ("less_or_equal", "=2<=2", {}, True),
    ("equal_numbers", "=1=1", {}, True),
    ("not_equal", "=1<>2", {}, True),
    ("equal_text_case_insensitive", '="abc"="ABC"', {}, True),
    # ---- concatenation -------------------------------------------------------
    ("concat_operator", '="foo"&"bar"', {}, "foobar"),
    ("concat_operator_with_number", '="x"&1', {}, "x1"),
    # ---- coercion (requirement #5) --------------------------------------------
    ("text_number_addition", '="5"+1', {}, 6.0),
    ("boolean_true_addition", "=TRUE+1", {}, 2.0),
    ("boolean_false_addition", "=FALSE+1", {}, 1.0),
    ("non_numeric_text_addition_errors", '="abc"+1', {}, ErrorValue(ErrorCode.VALUE)),
    # ---- logical ---------------------------------------------------------------
    ("if_true_branch", '=IF(1<2,"yes","no")', {}, "yes"),
    ("if_false_branch", '=IF(1>2,"yes","no")', {}, "no"),
    ("if_omitted_false_branch", '=IF(1>2,"yes")', {}, False),
    (
        "if_does_not_evaluate_untaken_branch",
        '=IF(TRUE,"safe",1/0)',
        {},
        "safe",
    ),
    ("ifs_second_condition", '=IFS(1>2,"a",1<2,"b")', {}, "b"),
    ("ifs_no_match_is_na", '=IFS(1>2,"a")', {}, ErrorValue(ErrorCode.NOT_AVAILABLE)),
    ("and_all_true", "=AND(TRUE,TRUE)", {}, True),
    ("and_one_false", "=AND(TRUE,FALSE)", {}, False),
    ("or_one_true", "=OR(FALSE,TRUE)", {}, True),
    ("or_all_false", "=OR(FALSE,FALSE)", {}, False),
    ("not_true", "=NOT(TRUE)", {}, False),
    # ---- aggregates --------------------------------------------------------
    ("sum_range", "=SUM(A1:A3)", {"A1": 1, "A2": 2, "A3": 3}, 6.0),
    ("sum_ignores_blank_from_reference", "=SUM(A1:A3)", {"A1": 1, "A3": 3}, 4.0),
    ("average_range", "=AVERAGE(A1:A3)", {"A1": 1, "A2": 2, "A3": 3}, 2.0),
    (
        "average_ignores_blank_from_reference",
        "=AVERAGE(A1:A3)",
        {"A1": 1, "A3": 3},
        2.0,
    ),
    ("min_range", "=MIN(A1:A3)", {"A1": 5, "A2": 1, "A3": 3}, 1.0),
    ("max_range", "=MAX(A1:A3)", {"A1": 5, "A2": 1, "A3": 3}, 5.0),
    ("count_ignores_text", "=COUNT(A1:A3)", {"A1": 1, "A2": "x", "A3": 3}, 2.0),
    ("counta_counts_text_and_numbers", "=COUNTA(A1:A3)", {"A1": 1, "A2": "x"}, 2.0),
    (
        "sumif_with_separate_sum_range",
        '=SUMIF(A1:A3,">1",B1:B3)',
        {"A1": 1, "A2": 2, "A3": 3, "B1": 10, "B2": 20, "B3": 30},
        50.0,
    ),
    (
        "countif_with_comparison_criteria",
        '=COUNTIF(A1:A3,">1")',
        {"A1": 1, "A2": 2, "A3": 3},
        2.0,
    ),
    # ---- lookups -------------------------------------------------------------
    (
        "vlookup_exact_match",
        "=VLOOKUP(2,A1:B3,2,FALSE)",
        {"A1": 1, "B1": "one", "A2": 2, "B2": "two", "A3": 3, "B3": "three"},
        "two",
    ),
    (
        "vlookup_exact_match_not_found",
        "=VLOOKUP(5,A1:B3,2,FALSE)",
        {"A1": 1, "B1": "one", "A2": 2, "B2": "two", "A3": 3, "B3": "three"},
        ErrorValue(ErrorCode.NOT_AVAILABLE),
    ),
    (
        "vlookup_approximate_match",
        "=VLOOKUP(2.5,A1:B3,2,TRUE)",
        {"A1": 1, "B1": "one", "A2": 2, "B2": "two", "A3": 3, "B3": "three"},
        "two",
    ),
    (
        "hlookup_exact_match",
        "=HLOOKUP(2,A1:C2,2,FALSE)",
        {"A1": 1, "B1": 2, "C1": 3, "A2": "one", "B2": "two", "C2": "three"},
        "two",
    ),
    (
        "index_two_dimensional",
        "=INDEX(A1:B2,2,2)",
        {"A1": 1, "B1": 2, "A2": 3, "B2": 4},
        4.0,
    ),
    (
        "match_exact",
        "=MATCH(20,A1:A3,0)",
        {"A1": 10, "A2": 20, "A3": 30},
        2.0,
    ),
    (
        "match_not_found_is_na",
        "=MATCH(99,A1:A3,0)",
        {"A1": 10, "A2": 20, "A3": 30},
        ErrorValue(ErrorCode.NOT_AVAILABLE),
    ),
    # ---- text -----------------------------------------------------------------
    ("concat_function", '=CONCAT("a","b","c")', {}, "abc"),
    ("left_default_one_character", '=LEFT("hello")', {}, "h"),
    ("left_n_characters", '=LEFT("hello",3)', {}, "hel"),
    ("right_n_characters", '=RIGHT("hello",2)', {}, "lo"),
    ("mid_substring", '=MID("hello",2,3)', {}, "ell"),
    ("trim_collapses_whitespace", '=TRIM("  a   b  ")', {}, "a b"),
    ("upper_case", '=UPPER("abc")', {}, "ABC"),
    ("lower_case", '=LOWER("ABC")', {}, "abc"),
    ("substitute_all", '=SUBSTITUTE("aXbXc","X","-")', {}, "a-b-c"),
    ("substitute_nth_instance", '=SUBSTITUTE("aXbXc","X","-",2)', {}, "aXb-c"),
    # ---- date/time -----------------------------------------------------------
    ("date_constructor", "=DATE(2026,7,28)", {}, datetime.date(2026, 7, 28)),
    ("year_of_date", "=YEAR(DATE(2026,7,28))", {}, 2026.0),
    ("month_of_date", "=MONTH(DATE(2026,7,28))", {}, 7.0),
    ("day_of_date", "=DAY(DATE(2026,7,28))", {}, 28.0),
    ("date_rolls_over_month", "=MONTH(DATE(2026,13,1))", {}, 1.0),
    ("date_rolls_over_year", "=YEAR(DATE(2026,13,1))", {}, 2027.0),
    ("time_constructor_is_a_day_fraction", "=TIME(12,30,0)", {}, 0.5208333333333334),
    ("hour_of_time", "=HOUR(TIME(12,30,0))", {}, 12.0),
    ("minute_of_time", "=MINUTE(TIME(12,30,0))", {}, 30.0),
    ("second_of_time", "=SECOND(TIME(12,30,15))", {}, 15.0),
    ("now_reads_injected_now", "=NOW()", {}, DEFAULT_NOW),
    ("today_reads_injected_now", "=TODAY()", {}, DEFAULT_NOW.date()),
    ("weekday_default_sunday_start", "=WEEKDAY(DATE(2026,7,28))", {}, 3.0),
    ("weekday_monday_start", "=WEEKDAY(DATE(2026,7,28),2)", {}, 2.0),
    # ---- math ------------------------------------------------------------------
    ("round_half_up", "=ROUND(2.5,0)", {}, 3.0),
    ("round_half_away_from_zero_negative", "=ROUND(-2.5,0)", {}, -3.0),
    ("round_two_digits", "=ROUND(3.14159,2)", {}, 3.14),
    ("abs_negative", "=ABS(-5)", {}, 5.0),
    ("mod_positive", "=MOD(7,3)", {}, 1.0),
    ("mod_negative_dividend_matches_divisor_sign", "=MOD(-7,3)", {}, 2.0),
    ("mod_by_zero", "=MOD(7,0)", {}, ErrorValue(ErrorCode.DIV0)),
    ("power_function", "=POWER(2,10)", {}, 1024.0),
    ("sqrt_positive", "=SQRT(16)", {}, 4.0),
    ("sqrt_negative_is_num_error", "=SQRT(-1)", {}, ErrorValue(ErrorCode.NUMBER)),
    # ---- the error lattice itself ----------------------------------------------
    ("literal_na_error", "=#N/A", {}, ErrorValue(ErrorCode.NOT_AVAILABLE)),
    (
        "unknown_function_is_name_error",
        "=NOSUCHFUNCTION(1)",
        {},
        ErrorValue(ErrorCode.NAME),
    ),
    (
        "non_coercible_literal_argument_is_value_error",
        '=SUM(1,2,"x")',
        {},
        ErrorValue(ErrorCode.VALUE),
    ),
    ("error_propagates_through_arithmetic", "=(1/0)+1", {}, ErrorValue(ErrorCode.DIV0)),
    (
        "error_propagates_through_if_condition",
        '=IF(1/0>0,"a","b")',
        {},
        ErrorValue(ErrorCode.DIV0),
    ),
]


@pytest.mark.parametrize(
    "formula, cells, expected",
    [
        pytest.param(formula, cells, expected, id=case_id)
        for case_id, formula, cells, expected in GOLDEN_CASES
    ],
)
def test_golden_formula_evaluates_to_expected_value(formula, cells, expected):
    assert evaluate_formula(formula, cells) == expected


def test_golden_suite_covers_every_registered_function():
    """A cheap guard against the golden suite silently drifting out of sync
    with the registry as functions are added — every registered function
    name must appear (as an uppercase token) in at least one golden
    formula."""
    from plugins.office.office.sheet.functions import FUNCTION_REGISTRY

    exercised_formulas = " ".join(formula.upper() for _, formula, _, _ in GOLDEN_CASES)
    missing = [name for name in FUNCTION_REGISTRY if name not in exercised_formulas]
    assert not missing, f"golden suite never exercises: {missing}"
