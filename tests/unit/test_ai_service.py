"""S147-3 unit coverage for ``OfficeAiService`` — tests #6 (unknown
capability rejected), #7 (budget exhausted -> no provider call), and #9 (a
provider failure degrades to a clear typed error, never a partial write —
this service never writes document content at all). Against fakes (no DB,
no network)."""
from datetime import datetime

import pytest

from vbwd.llm.errors import LlmError
from vbwd.services.llm_connection_service import NoActiveLlmConnectionError

from plugins.office.office.services.ai_service import OfficeAiService
from plugins.office.office.services.exceptions import (
    OfficeAiBudgetExceededError,
    OfficeAiInvalidCapabilityError,
    OfficeAiProviderError,
)


class FakeLlmClient:
    def __init__(self, reply="a proposed rewrite", *, raises=None):
        self._reply = reply
        self._raises = raises
        self.calls = []

    def chat(self, messages, *, system_prompt=None, temperature=None):
        self.calls.append({"messages": messages, "system_prompt": system_prompt})
        if self._raises is not None:
            raise self._raises
        return self._reply


class FakeLlmConnectionService:
    def __init__(self, client=None, *, known_slugs=("configured-slug",)):
        self._client = client or FakeLlmClient()
        self._known_slugs = set(known_slugs)
        self.requested_slugs = []

    def llm_client(self, slug=None):
        self.requested_slugs.append(slug)
        if slug is not None and slug not in self._known_slugs:
            raise NoActiveLlmConnectionError(
                f"No active LLM connection for slug '{slug}'"
            )
        return self._client


class FakeAiCallRepository:
    def __init__(self):
        self.calls = []

    def add(self, call):
        self.calls.append(call)
        return call

    def count_since(self, user_id, since_datetime):
        return len([c for c in self.calls if c.user_id == user_id])


def _service(**overrides):
    defaults = dict(
        llm_connection_service=FakeLlmConnectionService(),
        ai_call_repository=FakeAiCallRepository(),
        connection_slug="",
        default_monthly_call_budget=3,
        max_selection_chars=8000,
        max_context_chars=2000,
        now_provider=lambda: datetime(2026, 7, 27, 12, 0, 0),
    )
    defaults.update(overrides)
    return OfficeAiService(**defaults), defaults


def test_unknown_capability_is_rejected_before_any_provider_call():
    service, kwargs = _service()
    with pytest.raises(OfficeAiInvalidCapabilityError):
        service.run_capability(
            "user-1", "node-1", "delete_everything", selection_text="hi"
        )
    assert kwargs["ai_call_repository"].calls == []
    assert kwargs["llm_connection_service"].requested_slugs == []


def test_a_capability_requiring_selection_rejects_an_empty_one():
    service, _ = _service()
    with pytest.raises(OfficeAiInvalidCapabilityError):
        service.run_capability("user-1", "node-1", "summarize", selection_text="   ")


def test_continue_writing_allows_an_empty_selection():
    service, _ = _service()
    proposed_text, slug = service.run_capability(
        "user-1",
        "node-1",
        "continue_writing",
        selection_text="",
        context_before="Once upon a time",
    )
    assert proposed_text
    assert slug == "default"


def test_translate_rejects_an_unsupported_target_language():
    service, _ = _service()
    with pytest.raises(OfficeAiInvalidCapabilityError):
        service.run_capability(
            "user-1",
            "node-1",
            "translate",
            selection_text="hello",
            target_language="klingon",
        )


def test_budget_exhausted_raises_and_makes_no_provider_call():
    repository = FakeAiCallRepository()
    connection_service = FakeLlmConnectionService()
    service, _ = _service(
        ai_call_repository=repository,
        llm_connection_service=connection_service,
        default_monthly_call_budget=1,
    )
    service.run_capability(
        "user-1", "node-1", "summarize", selection_text="hi"
    )  # uses up the budget

    with pytest.raises(OfficeAiBudgetExceededError):
        service.run_capability(
            "user-1", "node-1", "summarize", selection_text="hi again"
        )

    # only ONE call reached the provider — the second, budget-exhausted
    # attempt made no provider call and was not recorded.
    assert len(connection_service.requested_slugs) == 1
    assert len(repository.calls) == 1


def test_a_configured_connection_slug_is_used_when_active():
    connection_service = FakeLlmConnectionService(known_slugs=("office-docs",))
    service, _ = _service(
        llm_connection_service=connection_service, connection_slug="office-docs"
    )
    _text, slug = service.run_capability(
        "user-1", "node-1", "summarize", selection_text="hi"
    )
    assert slug == "office-docs"
    assert connection_service.requested_slugs == ["office-docs"]


def test_an_unknown_configured_slug_degrades_to_the_default_connection():
    connection_service = FakeLlmConnectionService(known_slugs=("some-other-slug",))
    service, _ = _service(
        llm_connection_service=connection_service, connection_slug="typo-slug"
    )
    _text, slug = service.run_capability(
        "user-1", "node-1", "summarize", selection_text="hi"
    )
    assert slug == "default"
    # first call requested the (unknown) configured slug, then fell back
    assert connection_service.requested_slugs == ["typo-slug", None]


def test_provider_failure_raises_a_typed_error_and_records_an_error_call():
    repository = FakeAiCallRepository()
    failing_client = FakeLlmClient(raises=LlmError("boom"))
    connection_service = FakeLlmConnectionService(client=failing_client)
    service, _ = _service(
        ai_call_repository=repository, llm_connection_service=connection_service
    )

    with pytest.raises(OfficeAiProviderError):
        service.run_capability("user-1", "node-1", "summarize", selection_text="hi")

    assert len(repository.calls) == 1
    assert repository.calls[0].status == "error"
    assert repository.calls[0].tokens is None


def test_no_configured_connection_at_all_raises_a_clear_provider_error():
    class AlwaysFailingConnectionService:
        def llm_client(self, slug=None):
            raise NoActiveLlmConnectionError("No active LLM connection for default")

    service, _ = _service(llm_connection_service=AlwaysFailingConnectionService())
    with pytest.raises(OfficeAiProviderError):
        service.run_capability("user-1", "node-1", "summarize", selection_text="hi")


def test_a_successful_call_is_recorded_with_the_resolved_slug_and_tokens():
    repository = FakeAiCallRepository()
    service, _ = _service(ai_call_repository=repository, connection_slug="")
    service.run_capability(
        "user-1", "node-1", "summarize", selection_text="hello world"
    )

    assert len(repository.calls) == 1
    recorded = repository.calls[0]
    assert recorded.status == "success"
    assert recorded.connection_slug == "default"
    assert recorded.capability == "summarize"
    assert recorded.tokens and recorded.tokens > 0


def test_selection_and_context_are_capped_before_reaching_the_client():
    client = FakeLlmClient()
    connection_service = FakeLlmConnectionService(client=client)
    service, _ = _service(
        llm_connection_service=connection_service,
        max_selection_chars=10,
        max_context_chars=5,
    )
    service.run_capability(
        "user-1",
        "node-1",
        "summarize",
        selection_text="x" * 100,
        context_before="y" * 100,
        context_after="z" * 100,
    )
    sent_prompt = client.calls[0]["messages"][0]["content"]
    assert "x" * 11 not in sent_prompt
    assert "y" * 6 not in sent_prompt
    assert "z" * 6 not in sent_prompt


def test_the_client_never_sees_a_raw_prompt_field_shape():
    """The service's public contract has no ``prompt`` parameter at all —
    the route layer additionally rejects a ``prompt`` body field outright
    (see routes.py / the integration test)."""
    import inspect

    signature = inspect.signature(OfficeAiService.run_capability)
    assert "prompt" not in signature.parameters
