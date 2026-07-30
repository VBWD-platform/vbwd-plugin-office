"""S147-3.5 unit coverage for the Sheet AI prompt registry additions to
``ai_capabilities.py`` — the function-list-derivation contract (never a
second, hand-maintained function list) and the formula/text reply-shape
rules per capability."""
from plugins.office.office.sheet.functions import FUNCTION_REGISTRY
from plugins.office.office.services.ai_capabilities import (
    ALLOWED_CAPABILITY_IDS,
    CAPABILITIES_ALLOWING_EMPTY_SELECTION,
    CAPABILITY_SHEET_EXPLAIN_FORMULA,
    CAPABILITY_SHEET_FIX_ERROR,
    CAPABILITY_SHEET_SUMMARIZE_RANGE,
    CAPABILITY_SHEET_WRITE_FORMULA,
    FORMULA_PRODUCING_SHEET_CAPABILITIES,
    SHEET_CAPABILITY_IDS,
    allowed_sheet_function_names,
    build_prompt,
)


def test_all_four_sheet_capabilities_are_on_the_allow_list():
    assert SHEET_CAPABILITY_IDS <= ALLOWED_CAPABILITY_IDS
    assert SHEET_CAPABILITY_IDS == {
        CAPABILITY_SHEET_WRITE_FORMULA,
        CAPABILITY_SHEET_EXPLAIN_FORMULA,
        CAPABILITY_SHEET_SUMMARIZE_RANGE,
        CAPABILITY_SHEET_FIX_ERROR,
    }


def test_only_write_formula_and_fix_error_are_formula_producing():
    assert FORMULA_PRODUCING_SHEET_CAPABILITIES == {
        CAPABILITY_SHEET_WRITE_FORMULA,
        CAPABILITY_SHEET_FIX_ERROR,
    }


def test_write_formula_allows_an_empty_selection_but_others_do_not():
    assert CAPABILITY_SHEET_WRITE_FORMULA in CAPABILITIES_ALLOWING_EMPTY_SELECTION
    assert CAPABILITY_SHEET_EXPLAIN_FORMULA not in CAPABILITIES_ALLOWING_EMPTY_SELECTION
    assert CAPABILITY_SHEET_SUMMARIZE_RANGE not in CAPABILITIES_ALLOWING_EMPTY_SELECTION
    assert CAPABILITY_SHEET_FIX_ERROR not in CAPABILITIES_ALLOWING_EMPTY_SELECTION


def test_allowed_function_names_is_derived_from_the_live_registry():
    """The RED test this bundle names explicitly: the prompt's function
    list must never be a second, hand-maintained list that can drift from
    the engine's own registry."""
    assert set(allowed_sheet_function_names()) == set(FUNCTION_REGISTRY.keys())
    assert len(allowed_sheet_function_names()) == len(FUNCTION_REGISTRY)


def test_a_newly_registered_function_appears_in_the_prompt_without_a_second_edit():
    from plugins.office.office.sheet.functions._registry import register_function

    @register_function("ZZZTESTFUNC", 1, 1)
    def _zzz_test_func(arguments, context, evaluate):  # pragma: no cover - unused
        return 0.0

    try:
        system_prompt, _user_prompt = build_prompt(
            CAPABILITY_SHEET_WRITE_FORMULA,
            selection_text="A1\t1",
            context_before="",
            context_after="",
            intent="total column A",
        )
        assert "ZZZTESTFUNC" in system_prompt
    finally:
        del FUNCTION_REGISTRY["ZZZTESTFUNC"]


def test_write_formula_prompt_names_every_engine_function_and_asks_for_a_formula_only():
    system_prompt, user_prompt = build_prompt(
        CAPABILITY_SHEET_WRITE_FORMULA,
        selection_text="A1\t10\nA2\t20",
        context_before="",
        context_after="",
        intent="sum column A",
    )
    for function_name in FUNCTION_REGISTRY:
        assert function_name in system_prompt
    assert "starting with '='" in system_prompt
    assert "Intent: sum column A" in user_prompt
    assert "A1\t10" in user_prompt


def test_explain_formula_prompt_asks_for_prose_not_a_formula():
    system_prompt, _user_prompt = build_prompt(
        CAPABILITY_SHEET_EXPLAIN_FORMULA,
        selection_text="Active formula: =SUM(A1:A3)",
        context_before="",
        context_after="",
    )
    assert "explanation" in system_prompt.lower()
    assert "starting with '='" not in system_prompt


def test_summarize_range_prompt_asks_for_a_summary():
    system_prompt, user_prompt = build_prompt(
        CAPABILITY_SHEET_SUMMARIZE_RANGE,
        selection_text="A1\t10\nA2\t20\nA3\t30",
        context_before="",
        context_after="",
    )
    assert "trend" in system_prompt.lower() or "summary" in system_prompt.lower()
    assert "A1\t10" in user_prompt


def test_fix_error_prompt_asks_for_a_corrected_formula():
    system_prompt, _user_prompt = build_prompt(
        CAPABILITY_SHEET_FIX_ERROR,
        selection_text="Active formula: =A1/0",
        context_before="",
        context_after="",
    )
    assert "corrected formula" in system_prompt
    assert "starting with '='" in system_prompt
