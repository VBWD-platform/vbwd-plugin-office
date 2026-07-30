"""AI helper capability registry — server-side prompt templates (S147-3,
extended for VBWD Spreadsheets in S147-3.5).

The client sends a CAPABILITY ID (one of the constants below) plus the text
to act on; it never sends a raw prompt (``routes.py`` rejects a ``prompt``
field outright, before this module is even reached). Every instruction the
model receives is built HERE, from a fixed template — this is the control
that keeps VBWD Docs/Spreadsheets from becoming an open proxy to the
operator's paid LLM connection: the client chooses WHAT KIND of help it
wants, never WHAT THE MODEL IS TOLD TO DO.

The four ``sheet_*`` capabilities below are the ONLY free-text exception,
and only for ``sheet_write_formula``: expressing "total column B" needs a
short natural-language intent. It is still selected by capability id, still
budget-gated, and still length-capped exactly like ``selection_text``
(enforced in ``OfficeAiService.run_capability``) — it is never treated as an
instruction to the model beyond "the user's stated intent for this formula".
"""
from __future__ import annotations

from typing import Optional, Tuple

from plugins.office.office.sheet.functions import FUNCTION_REGISTRY

CAPABILITY_CONTINUE_WRITING = "continue_writing"
CAPABILITY_REWRITE_SHORTER = "rewrite_shorter"
CAPABILITY_REWRITE_LONGER = "rewrite_longer"
CAPABILITY_REWRITE_FORMAL = "rewrite_formal"
CAPABILITY_REWRITE_PLAIN = "rewrite_plain"
CAPABILITY_SUMMARIZE = "summarize"
CAPABILITY_FIX_GRAMMAR = "fix_grammar"
CAPABILITY_TRANSLATE = "translate"
CAPABILITY_OUTLINE = "outline"

#: VBWD Spreadsheets capabilities (S147-3.5). ``sheet_write_formula`` and
#: ``sheet_fix_error`` propose a FORMULA (never auto-applied — the client
#: accepts it through the existing, engine-validated ``save_cells`` path);
#: ``sheet_explain_formula``/``sheet_summarize_range`` return prose.
CAPABILITY_SHEET_WRITE_FORMULA = "sheet_write_formula"
CAPABILITY_SHEET_EXPLAIN_FORMULA = "sheet_explain_formula"
CAPABILITY_SHEET_SUMMARIZE_RANGE = "sheet_summarize_range"
CAPABILITY_SHEET_FIX_ERROR = "sheet_fix_error"

SHEET_CAPABILITY_IDS = frozenset(
    {
        CAPABILITY_SHEET_WRITE_FORMULA,
        CAPABILITY_SHEET_EXPLAIN_FORMULA,
        CAPABILITY_SHEET_SUMMARIZE_RANGE,
        CAPABILITY_SHEET_FIX_ERROR,
    }
)

#: The sheet capabilities whose model reply is a FORMULA (vs. prose) — the
#: orchestrator (``sheet_ai_service.py``) uses this to shape the returned
#: proposal, and the route uses it to require EDIT access rather than VIEW
#: (proposing a formula to apply needs edit; explaining/summarising does not).
FORMULA_PRODUCING_SHEET_CAPABILITIES = frozenset(
    {CAPABILITY_SHEET_WRITE_FORMULA, CAPABILITY_SHEET_FIX_ERROR}
)

ALLOWED_CAPABILITY_IDS = frozenset(
    {
        CAPABILITY_CONTINUE_WRITING,
        CAPABILITY_REWRITE_SHORTER,
        CAPABILITY_REWRITE_LONGER,
        CAPABILITY_REWRITE_FORMAL,
        CAPABILITY_REWRITE_PLAIN,
        CAPABILITY_SUMMARIZE,
        CAPABILITY_FIX_GRAMMAR,
        CAPABILITY_TRANSLATE,
        CAPABILITY_OUTLINE,
    }
    | SHEET_CAPABILITY_IDS
)

#: Capabilities that act on the whole document-so-far rather than a
#: selection (no text is "selected" to continue writing from, or to outline)
#: — these two may be called with an empty ``selection_text``.
#: ``sheet_write_formula`` joins them for the analogous reason: the active
#: cell may be brand new, with no populated nearby cells yet — the user's
#: ``intent`` (separately required, see ``OfficeAiService._validate_request``)
#: carries the instruction instead.
CAPABILITIES_ALLOWING_EMPTY_SELECTION = frozenset(
    {CAPABILITY_CONTINUE_WRITING, CAPABILITY_OUTLINE, CAPABILITY_SHEET_WRITE_FORMULA}
)

#: A small, explicit allow-list — ``translate`` needs a target, but it must
#: never become a free-text field the model is asked to "obey" (that would
#: reopen the same prompt-injection surface the capability-id design closes).
ALLOWED_TARGET_LANGUAGES = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
}

_SYSTEM_PROMPT = (
    "You are a writing assistant embedded in a rich-text document editor. "
    "You are given an instruction and a piece of text; you transform the "
    "text according to the instruction ONLY. Reply with ONLY the resulting "
    "plain text — no preamble, no explanation, no markdown code fences, no "
    "quotation marks around the result."
)

_INSTRUCTIONS = {
    CAPABILITY_CONTINUE_WRITING: (
        "Continue writing naturally from where the text so far leaves off. "
        "Write one to three sentences that plausibly continue it."
    ),
    CAPABILITY_REWRITE_SHORTER: (
        "Rewrite the selected text to be noticeably shorter, keeping its meaning."
    ),
    CAPABILITY_REWRITE_LONGER: (
        "Rewrite the selected text to be noticeably longer and more detailed, "
        "keeping its meaning."
    ),
    CAPABILITY_REWRITE_FORMAL: "Rewrite the selected text in a more formal register.",
    CAPABILITY_REWRITE_PLAIN: "Rewrite the selected text in plain, simple language.",
    CAPABILITY_SUMMARIZE: "Summarise the selected text in a few sentences.",
    CAPABILITY_FIX_GRAMMAR: (
        "Fix the spelling and grammar of the selected text without changing its "
        "meaning or tone."
    ),
    CAPABILITY_OUTLINE: (
        "Produce a short outline (as a bulleted list of lines) of the text so far."
    ),
}


def build_prompt(
    capability_id: str,
    *,
    selection_text: str,
    context_before: str,
    context_after: str,
    target_language: Optional[str] = None,
    intent: str = "",
) -> Tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for ``capability_id``. Callers
    must have already validated ``capability_id`` is on the allow-list."""
    if capability_id in SHEET_CAPABILITY_IDS:
        return _build_sheet_prompt(
            capability_id, selection_text=selection_text, intent=intent
        )
    instruction = _instruction_for(capability_id, target_language)
    user_prompt = (
        f"Instruction: {instruction}\n\n"
        f"--- Text before the selection (context only, do not repeat it) ---\n"
        f"{context_before}\n\n"
        f"--- Selected text (act on this) ---\n"
        f"{selection_text}\n\n"
        f"--- Text after the selection (context only, do not repeat it) ---\n"
        f"{context_after}\n"
    )
    return _SYSTEM_PROMPT, user_prompt


def _instruction_for(capability_id: str, target_language: Optional[str]) -> str:
    if capability_id == CAPABILITY_TRANSLATE:
        # Callers validate target_language is a known key before reaching here
        # (OfficeAiService._validate_request) — the assert only narrows the
        # type for mypy, it never changes runtime behaviour.
        assert target_language is not None
        language_name = ALLOWED_TARGET_LANGUAGES[target_language]
        return f"Translate the selected text into {language_name}."
    return _INSTRUCTIONS[capability_id]


# ---------------------------------------------------------------------------
# VBWD Spreadsheets (S147-3.5)
# ---------------------------------------------------------------------------


def allowed_sheet_function_names() -> Tuple[str, ...]:
    """The exact set of formula functions the engine implements, derived
    from the SAME registry ``office/sheet/functions`` decorates itself with
    at import time — never a second, hand-maintained list that can drift out
    of sync as functions are added (the "41-function registry" the sprint
    warns about). Sorted for a stable, readable prompt."""
    return tuple(sorted(FUNCTION_REGISTRY.keys()))


def _function_list_text() -> str:
    return ", ".join(allowed_sheet_function_names())


#: Reply-format rule shared by every sheet capability's system prompt —
#: keeping the model on the engine's exact function vocabulary is the single
#: control that stops it confidently returning e.g. ``XLOOKUP`` (not
#: implemented here) and producing a ``#NAME?`` error.
def _sheet_system_prompt(extra_instruction: str) -> str:
    return (
        "You are a spreadsheet formula assistant embedded in VBWD "
        "Spreadsheets. The calculation engine implements EXACTLY these "
        f"functions, and no others: {_function_list_text()}. Never propose "
        "a function outside this list — it would evaluate to a #NAME? "
        "error. " + extra_instruction
    )


_SHEET_FORMULA_REPLY_RULE = (
    "Reply with ONLY a single spreadsheet formula for the active cell, "
    "starting with '='. No explanation, no markdown code fences, no "
    "quotation marks."
)

_SHEET_TEXT_REPLY_RULE = (
    "Reply with ONLY the explanation itself, in plain prose — no markdown, "
    "no preamble, no quotation marks."
)

#: One extra-instruction fragment per sheet capability — kept as plain
#: strings (not pre-built system prompts) so the function list is spliced in
#: freshly by :func:`_sheet_system_prompt` on every call, never baked in at
#: import time (a function registered after this module first imports must
#: still appear the very next time a prompt is built).
_SHEET_EXTRA_INSTRUCTIONS = {
    CAPABILITY_SHEET_WRITE_FORMULA: (
        "Given the user's stated intent and the surrounding cell values, "
        "propose a formula for the active cell that achieves that intent. "
        + _SHEET_FORMULA_REPLY_RULE
    ),
    CAPABILITY_SHEET_FIX_ERROR: (
        "The active cell's current formula evaluates to an error. Propose a "
        "corrected formula that fixes the error while preserving the "
        "original formula's intent as closely as possible. " + _SHEET_FORMULA_REPLY_RULE
    ),
    CAPABILITY_SHEET_EXPLAIN_FORMULA: (
        "Explain in plain language, in two to four sentences, what the "
        "active cell's formula computes. " + _SHEET_TEXT_REPLY_RULE
    ),
    CAPABILITY_SHEET_SUMMARIZE_RANGE: (
        "Describe what the selected data shows: totals, notable trends, and "
        "outliers, in three to six sentences. " + _SHEET_TEXT_REPLY_RULE
    ),
}


def _build_sheet_prompt(
    capability_id: str, *, selection_text: str, intent: str
) -> Tuple[str, str]:
    system_prompt = _sheet_system_prompt(_SHEET_EXTRA_INSTRUCTIONS[capability_id])
    if capability_id == CAPABILITY_SHEET_WRITE_FORMULA:
        user_prompt = (
            f"Intent: {intent}\n\n"
            f"--- Active cell and nearby cells ---\n{selection_text}\n"
        )
    else:
        user_prompt = f"--- Active cell and selected range ---\n{selection_text}\n"
    return system_prompt, user_prompt
