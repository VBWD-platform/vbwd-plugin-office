"""S147-3.5 unit coverage for the Sheet AI prompt registry additions to
``ai_capabilities.py`` — the function-list-derivation contract (never a
second, hand-maintained function list) and the formula/text reply-shape
rules per capability."""
from plugins.office.office.sheet.functions import FUNCTION_REGISTRY
from plugins.office.office.services.ai_capabilities import (
    ALLOWED_CAPABILITY_IDS,
    CAPABILITIES_ALLOWING_EMPTY_SELECTION,
    CAPABILITY_FREEFORM,
    CAPABILITY_SHEET_EXPLAIN_FORMULA,
    CAPABILITY_SHEET_FIX_ERROR,
    CAPABILITY_SHEET_FREEFORM,
    CAPABILITY_SHEET_SUMMARIZE_RANGE,
    CAPABILITY_SHEET_WRITE_FORMULA,
    FORMULA_PRODUCING_SHEET_CAPABILITIES,
    FREEFORM_CAPABILITY_IDS,
    SHEET_CAPABILITY_IDS,
    allowed_sheet_function_names,
    build_prompt,
)


def test_all_five_sheet_capabilities_are_on_the_allow_list():
    assert SHEET_CAPABILITY_IDS <= ALLOWED_CAPABILITY_IDS
    assert SHEET_CAPABILITY_IDS == {
        CAPABILITY_SHEET_WRITE_FORMULA,
        CAPABILITY_SHEET_EXPLAIN_FORMULA,
        CAPABILITY_SHEET_SUMMARIZE_RANGE,
        CAPABILITY_SHEET_FIX_ERROR,
        CAPABILITY_SHEET_FREEFORM,
    }


def test_freeform_capability_ids_are_exactly_the_two_free_text_capabilities():
    assert CAPABILITY_FREEFORM == "freeform"
    assert CAPABILITY_SHEET_FREEFORM == "sheet_freeform"
    assert FREEFORM_CAPABILITY_IDS == {CAPABILITY_FREEFORM, CAPABILITY_SHEET_FREEFORM}
    assert CAPABILITY_FREEFORM in ALLOWED_CAPABILITY_IDS
    assert CAPABILITY_SHEET_FREEFORM in ALLOWED_CAPABILITY_IDS
    # sheet_freeform is NOT statically formula-producing — its shape is
    # decided per-reply (dynamic), unlike sheet_write_formula/sheet_fix_error.
    assert CAPABILITY_SHEET_FREEFORM not in FORMULA_PRODUCING_SHEET_CAPABILITIES


def test_freeform_capabilities_allow_an_empty_selection():
    # A freeform prompt may stand on its own (e.g. "outline the next
    # section" for Docs, or a brand-new active cell for Sheets) — the
    # prompt itself carries the intent, exactly like sheet_write_formula's
    # ``intent``.
    assert CAPABILITY_FREEFORM in CAPABILITIES_ALLOWING_EMPTY_SELECTION
    assert CAPABILITY_SHEET_FREEFORM in CAPABILITIES_ALLOWING_EMPTY_SELECTION


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


def test_freeform_doc_prompt_carries_the_users_prompt_as_the_instruction():
    system_prompt, user_prompt = build_prompt(
        CAPABILITY_FREEFORM,
        selection_text="The quick brown fox.",
        context_before="Once upon a time.",
        context_after="The end.",
        prompt="Rewrite this as three bullet points",
    )
    assert "Rewrite this as three bullet points" in user_prompt
    assert "The quick brown fox." in user_prompt
    assert "Once upon a time." in user_prompt
    # The system prompt gives no fixed instruction of its own — the user's
    # prompt IS the instruction (this is what distinguishes freeform from
    # every preset capability).
    assert "resulting plain" in system_prompt.lower()


def test_freeform_sheet_prompt_names_every_engine_function_and_carries_the_prompt():
    system_prompt, user_prompt = build_prompt(
        CAPABILITY_SHEET_FREEFORM,
        selection_text="A1\t10\nB1\t2",
        context_before="",
        context_after="",
        prompt="add a column that is column B times 2",
    )
    for function_name in FUNCTION_REGISTRY:
        assert function_name in system_prompt
    assert "add a column that is column B times 2" in user_prompt
    assert "A1\t10" in user_prompt
    # Freeform must stay able to reply with EITHER a formula or prose,
    # unlike the single-shape preset sheet capabilities.
    assert "formula" in system_prompt.lower()
