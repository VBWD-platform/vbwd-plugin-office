"""AI helper capability registry — server-side prompt templates (S147-3).

The client sends a CAPABILITY ID (one of the constants below) plus the text
to act on; it never sends a raw prompt (``routes.py`` rejects a ``prompt``
field outright, before this module is even reached). Every instruction the
model receives is built HERE, from a fixed template — this is the control
that keeps VBWD Docs from becoming an open proxy to the operator's paid LLM
connection: the client chooses WHAT KIND of help it wants, never WHAT THE
MODEL IS TOLD TO DO.
"""
from __future__ import annotations

from typing import Optional, Tuple

CAPABILITY_CONTINUE_WRITING = "continue_writing"
CAPABILITY_REWRITE_SHORTER = "rewrite_shorter"
CAPABILITY_REWRITE_LONGER = "rewrite_longer"
CAPABILITY_REWRITE_FORMAL = "rewrite_formal"
CAPABILITY_REWRITE_PLAIN = "rewrite_plain"
CAPABILITY_SUMMARIZE = "summarize"
CAPABILITY_FIX_GRAMMAR = "fix_grammar"
CAPABILITY_TRANSLATE = "translate"
CAPABILITY_OUTLINE = "outline"

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
)

#: Capabilities that act on the whole document-so-far rather than a
#: selection (no text is "selected" to continue writing from, or to outline)
#: — these two may be called with an empty ``selection_text``.
CAPABILITIES_ALLOWING_EMPTY_SELECTION = frozenset(
    {CAPABILITY_CONTINUE_WRITING, CAPABILITY_OUTLINE}
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
) -> Tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for ``capability_id``. Callers
    must have already validated ``capability_id`` is on the allow-list."""
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
