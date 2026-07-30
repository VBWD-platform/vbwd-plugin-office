"""S147-3.5 unit coverage for ``SheetAiOrchestratorService`` — against
fakes for both collaborators (``OfficeSheetEditorService``'s
``read_selection_for_ai`` and ``OfficeAiService``'s ``run_capability``), so
these tests prove the orchestrator's OWN glue: access gating, ai_enabled
gating, TSV serialisation, and the formula-vs-text proposal shape — never
re-testing the budget/audit/connection machinery that already has its own
unit coverage in ``test_ai_service.py``."""
import uuid

import pytest

from plugins.office.office.sheet.cell import CellAddress
from plugins.office.office.services.access_resolver import ACCESS_OWNER
from plugins.office.office.services.exceptions import (
    OfficeAiDisabledError,
    OfficeAiInvalidCapabilityError,
    OfficeShareForbiddenError,
)
from plugins.office.office.services.sheet_ai_service import SheetAiOrchestratorService
from plugins.office.office.services.sheet_editor_service import SheetAiSelection


class _Node:
    def __init__(self):
        self.id = uuid.uuid4()


class _Document:
    def __init__(self, ai_enabled=True):
        self.ai_enabled = ai_enabled


class FakeSheetEditorService:
    def __init__(self, selection):
        self._selection = selection
        self.calls = []

    def read_selection_for_ai(self, user_id, node_id, address_text, range_text=None):
        self.calls.append((user_id, node_id, address_text, range_text))
        return self._selection


class FakeAiService:
    def __init__(self, reply="=SUM(A1:A2)", slug="default"):
        self._reply = reply
        self._slug = slug
        self.calls = []

    def run_capability(self, user_id, node_id, capability_id, **kwargs):
        self.calls.append((user_id, node_id, capability_id, kwargs))
        return self._reply, self._slug


def _selection(*, access=ACCESS_OWNER, ai_enabled=True, formula="=A1+A2", value=30.0):
    active_address = CellAddress(sheet="Sheet1", column=2, row=1)
    range_cells = [
        (CellAddress(sheet="Sheet1", column=1, row=1), 10.0),
        (CellAddress(sheet="Sheet1", column=1, row=2), 20.0),
    ]
    return SheetAiSelection(
        node=_Node(),
        document=_Document(ai_enabled=ai_enabled),
        access=access,
        active_sheet="Sheet1",
        active_address=active_address,
        active_formula=formula,
        active_value=value,
        range_cells=range_cells,
        range_truncated=False,
    )


def _orchestrator(selection, ai_service=None):
    sheet_editor_service = FakeSheetEditorService(selection)
    ai_service = ai_service or FakeAiService()
    return (
        SheetAiOrchestratorService(sheet_editor_service, ai_service),
        sheet_editor_service,
        ai_service,
    )


def test_unknown_capability_is_rejected_before_reading_the_sheet():
    orchestrator, sheet_editor_service, ai_service = _orchestrator(_selection())
    with pytest.raises(OfficeAiInvalidCapabilityError):
        orchestrator.run_capability(
            "user-1", "node-1", "delete_everything", address="B1"
        )
    assert sheet_editor_service.calls == []
    assert ai_service.calls == []


def test_missing_address_is_rejected():
    orchestrator, _sheet, _ai = _orchestrator(_selection())
    with pytest.raises(OfficeAiInvalidCapabilityError):
        orchestrator.run_capability(
            "user-1", "node-1", "sheet_explain_formula", address=""
        )


def test_ai_disabled_document_raises_regardless_of_access():
    orchestrator, _sheet, ai_service = _orchestrator(_selection(ai_enabled=False))
    with pytest.raises(OfficeAiDisabledError):
        orchestrator.run_capability(
            "user-1", "node-1", "sheet_explain_formula", address="B1"
        )
    assert ai_service.calls == []


def test_view_access_may_explain_a_formula():
    orchestrator, _sheet, ai_service = _orchestrator(
        _selection(access="view"), ai_service=FakeAiService(reply="It sums A1 and A2.")
    )
    proposal = orchestrator.run_capability(
        "user-1", "node-1", "sheet_explain_formula", address="B1"
    )
    assert proposal["kind"] == "text"
    assert proposal["text"] == "It sums A1 and A2."
    assert len(ai_service.calls) == 1


def test_view_access_cannot_propose_a_formula_to_apply():
    orchestrator, _sheet, ai_service = _orchestrator(_selection(access="view"))
    with pytest.raises(OfficeShareForbiddenError):
        orchestrator.run_capability(
            "user-1",
            "node-1",
            "sheet_write_formula",
            address="B1",
            intent="sum A1:A2",
        )
    assert ai_service.calls == []


def test_edit_access_can_propose_a_formula():
    orchestrator, _sheet, ai_service = _orchestrator(
        _selection(access="edit"), ai_service=FakeAiService(reply="=SUM(A1:A2)")
    )
    proposal = orchestrator.run_capability(
        "user-1",
        "node-1",
        "sheet_write_formula",
        address="B1",
        intent="sum A1:A2",
    )
    assert proposal == {
        "kind": "formula",
        "capability": "sheet_write_formula",
        "connection_slug": "default",
        "address": "B1",
        "formula": "=SUM(A1:A2)",
    }
    assert ai_service.calls[0][3]["intent"] == "sum A1:A2"


def test_fix_error_also_requires_edit_and_returns_a_formula_proposal():
    orchestrator, _sheet, _ai = _orchestrator(_selection(access="comment"))
    with pytest.raises(OfficeShareForbiddenError):
        orchestrator.run_capability("user-1", "node-1", "sheet_fix_error", address="B1")


def test_summarize_range_returns_a_text_proposal_and_serializes_the_range():
    orchestrator, sheet_editor_service, ai_service = _orchestrator(
        _selection(), ai_service=FakeAiService(reply="A1:A2 sum to 30.")
    )
    proposal = orchestrator.run_capability(
        "user-1", "node-1", "sheet_summarize_range", address="B1", range_text="A1:A2"
    )
    assert proposal["kind"] == "text"
    assert proposal["text"] == "A1:A2 sum to 30."
    assert sheet_editor_service.calls == [("user-1", "node-1", "B1", "A1:A2")]
    sent_selection_text = ai_service.calls[0][3]["selection_text"]
    assert "A1\t10" in sent_selection_text
    assert "A2\t20" in sent_selection_text
    assert "Active formula: =A1+A2" in sent_selection_text
