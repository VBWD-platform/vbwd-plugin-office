"""``OfficeAiService`` — the S147-3 AI helper orchestrator.

Rides the CORE LLM Connection Manager
(``vbwd.services.llm_connection_service.LlmConnectionService``) — no
provider code, no key handling in this plugin. The connection is addressed
by SLUG, never an id (an id resolves to the wrong connection on another box).
An unknown/inactive configured slug degrades to the active DEFAULT
connection rather than erroring — core's own resolver raises for a bad slug
by design (it is a real admin-facing signal there), so THIS service is the
one that implements the plugin-level "degrade, don't error" contract by
catching that specific failure and retrying with no slug.

Controls implemented here, mirroring the sprint doc's table:

* Capability id allow-list (delegated to ``ai_capabilities``) — never a raw
  prompt.
* Per-user monthly call budget from the entitlement seam; ``429`` past it,
  and NO provider call is made (checked before resolving a client).
* Only the selection + a bounded window of surrounding text is forwarded to
  the LLM — both are hard-capped in characters here, independent of whatever
  length the client sent.
* Every call that actually reaches the provider is recorded (user,
  capability, an approximate token count, connection slug); a
  budget-exhausted call is never recorded (nothing was attempted).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from vbwd.llm.errors import LlmError
from vbwd.services.entitlement import resolve_entitlement_provider
from vbwd.services.llm_connection_service import NoActiveLlmConnectionError

from plugins.office.office.models.office_ai_call import (
    STATUS_ERROR,
    STATUS_SUCCESS,
    OfficeAiCall,
)
from plugins.office.office.services.ai_capabilities import (
    ALLOWED_CAPABILITY_IDS,
    ALLOWED_TARGET_LANGUAGES,
    CAPABILITIES_ALLOWING_EMPTY_SELECTION,
    CAPABILITY_SHEET_WRITE_FORMULA,
    CAPABILITY_TRANSLATE,
    build_prompt,
)
from plugins.office.office.services.exceptions import (
    OfficeAiBudgetExceededError,
    OfficeAiInvalidCapabilityError,
    OfficeAiProviderError,
)

#: The entitlement feature key a paid plan can override (mirrors
#: ``QuotaService.QUOTA_FEATURE_KEY``'s pattern exactly).
AI_BUDGET_FEATURE_KEY = "office.ai_monthly_call_budget"

_DEFAULT_CONNECTION_LABEL = "default"

#: Approximates tokens from characters — the core adapters
#: (``vbwd/llm/adapter.py``) do not return provider usage counts, so this is
#: a deterministic, documented heuristic, never presented as exact.
_CHARS_PER_TOKEN_ESTIMATE = 4


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN_ESTIMATE)


class OfficeAiService:
    def __init__(
        self,
        llm_connection_service,
        ai_call_repository,
        *,
        connection_slug: str = "",
        default_monthly_call_budget: int,
        max_selection_chars: int,
        max_context_chars: int,
        now_provider=datetime.utcnow,
    ) -> None:
        self._llm_connection_service = llm_connection_service
        self._ai_call_repository = ai_call_repository
        self._connection_slug = connection_slug or None
        self._default_monthly_call_budget = default_monthly_call_budget
        self._max_selection_chars = max_selection_chars
        self._max_context_chars = max_context_chars
        self._now_provider = now_provider

    def run_capability(
        self,
        user_id,
        node_id,
        capability_id: str,
        *,
        selection_text: str = "",
        context_before: str = "",
        context_after: str = "",
        target_language: Optional[str] = None,
        intent: str = "",
    ) -> Tuple[str, str]:
        """Return ``(proposed_text, connection_slug)``. Raises a typed
        exception (never a bare 500) for every rejection path.

        ``intent`` is the one free-text field a client may send (only
        meaningful for ``sheet_write_formula``) — capped in CHARACTERS to
        the exact same ``max_selection_chars`` bound as ``selection_text``,
        never trusted at whatever length the client sent."""
        self._validate_request(capability_id, selection_text, target_language, intent)

        selection_text = selection_text[: self._max_selection_chars]
        context_before = (context_before or "")[: self._max_context_chars]
        context_after = (context_after or "")[: self._max_context_chars]
        intent = (intent or "")[: self._max_selection_chars]

        self._enforce_budget(user_id)

        system_prompt, user_prompt = build_prompt(
            capability_id,
            selection_text=selection_text,
            context_before=context_before,
            context_after=context_after,
            target_language=target_language,
            intent=intent,
        )

        client, connection_slug = self._resolve_client()
        try:
            proposed_text = client.chat(
                [{"role": "user", "content": user_prompt}], system_prompt=system_prompt
            )
        except LlmError as llm_error:
            self._record_call(
                user_id, node_id, capability_id, connection_slug, None, STATUS_ERROR
            )
            raise OfficeAiProviderError(str(llm_error)) from llm_error

        tokens_estimate = _estimate_tokens(user_prompt) + _estimate_tokens(
            proposed_text
        )
        self._record_call(
            user_id,
            node_id,
            capability_id,
            connection_slug,
            tokens_estimate,
            STATUS_SUCCESS,
        )
        return proposed_text.strip(), connection_slug

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _validate_request(
        self,
        capability_id: str,
        selection_text: str,
        target_language: Optional[str],
        intent: str,
    ) -> None:
        if capability_id not in ALLOWED_CAPABILITY_IDS:
            raise OfficeAiInvalidCapabilityError(
                f"Unknown capability: {capability_id!r}"
            )
        requires_selection = capability_id not in CAPABILITIES_ALLOWING_EMPTY_SELECTION
        if requires_selection and not (selection_text or "").strip():
            raise OfficeAiInvalidCapabilityError(
                f"'{capability_id}' requires a non-empty selection_text"
            )
        if (
            capability_id == CAPABILITY_TRANSLATE
            and target_language not in ALLOWED_TARGET_LANGUAGES
        ):
            raise OfficeAiInvalidCapabilityError(
                f"Unsupported target_language: {target_language!r}"
            )
        if (
            capability_id == CAPABILITY_SHEET_WRITE_FORMULA
            and not (intent or "").strip()
        ):
            raise OfficeAiInvalidCapabilityError(
                f"'{capability_id}' requires a non-empty intent"
            )

    def _enforce_budget(self, user_id) -> None:
        budget = self._monthly_call_budget(user_id)
        since = self._start_of_current_month()
        used = self._ai_call_repository.count_since(user_id, since)
        if used >= budget:
            raise OfficeAiBudgetExceededError(user_id, used, budget)

    def _monthly_call_budget(self, user_id) -> int:
        provider = resolve_entitlement_provider()
        value = provider.get_feature_value(
            user_id, AI_BUDGET_FEATURE_KEY, default=self._default_monthly_call_budget
        )
        return int(value) if value is not None else self._default_monthly_call_budget

    def _start_of_current_month(self) -> datetime:
        now = self._now_provider()
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _resolve_client(self):
        """Resolve a ready ``LlmClient`` for the configured slug, degrading
        to the active default when that slug is missing/inactive (never for
        an outright missing configuration — that propagates as a clear
        error)."""
        try:
            client = self._llm_connection_service.llm_client(slug=self._connection_slug)
            return client, (self._connection_slug or _DEFAULT_CONNECTION_LABEL)
        except NoActiveLlmConnectionError as resolve_error:
            if self._connection_slug is None:
                raise OfficeAiProviderError(
                    "No LLM connection is configured for VBWD Docs"
                ) from resolve_error
            try:
                client = self._llm_connection_service.llm_client(slug=None)
            except NoActiveLlmConnectionError as fallback_error:
                raise OfficeAiProviderError(
                    "No LLM connection is configured for VBWD Docs"
                ) from fallback_error
            return client, _DEFAULT_CONNECTION_LABEL

    def _record_call(
        self, user_id, node_id, capability_id, connection_slug, tokens, status
    ) -> None:
        self._ai_call_repository.add(
            OfficeAiCall(
                node_id=node_id,
                user_id=user_id,
                capability=capability_id,
                connection_slug=connection_slug,
                tokens=tokens,
                status=status,
            )
        )
