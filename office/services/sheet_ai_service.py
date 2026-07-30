"""``SheetAiOrchestratorService`` — the S147-3.5 VBWD Spreadsheets AI helper
orchestrator.

A THIN service: it turns Sheet state into a call against the SHARED
``OfficeAiService`` (S147-3's AI helper machinery — capability allow-list,
per-user monthly budget, LLM Connection Manager resolution, selection
char-caps, and the audit trail) and turns the model's reply back into a
STRUCTURED proposal. It adds NO second budget/audit/connection/validation
path — every one of those controls still lives, exactly once, in
``OfficeAiService``.

The only Sheet-specific work here is: resolving access + reading a BOUNDED
slice of the recalculated workbook through
``OfficeSheetEditorService.read_selection_for_ai`` (never the whole
workbook — bounded by that method's own cell cap, then again by
``OfficeAiService``'s character cap: cost control and data minimisation
twice over), serialising it as a compact TSV table, and mapping the model's
reply onto a typed proposal the client can act on.

Proposals are **accept-only**: a ``formula`` proposal is never written to
storage from here — the client applies it through the EXISTING
``PUT /sheets/<node_id>/cells`` path, which is the only place a formula is
parsed, validated, and recalculated by the engine. If the model's reply is
not a parseable formula, ``save_cells`` turns it into a ``#NAME?``/400 on
apply, exactly like a human typing a bad formula — that is the correct,
already-tested outcome, not a new failure mode this module needs to guard
against.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from plugins.office.office.sheet.cell import CellAddress, format_cell_reference
from plugins.office.office.sheet.values import coerce_to_text, is_error

from plugins.office.office.services.ai_capabilities import (
    CAPABILITY_SHEET_FREEFORM,
    FORMULA_PRODUCING_SHEET_CAPABILITIES,
    SHEET_CAPABILITY_IDS,
)
from plugins.office.office.services.exceptions import (
    OfficeAiDisabledError,
    OfficeAiInvalidCapabilityError,
    OfficeShareForbiddenError,
)
from plugins.office.office.services.sheet_editor_service import EDIT_CAPABLE_ACCESS

PROPOSAL_KIND_FORMULA = "formula"
PROPOSAL_KIND_TEXT = "text"


@dataclass(frozen=True)
class SheetAiProposal:
    """The structured contract returned to the client — never prose to
    parse client-side. A ``formula`` proposal names the ``address`` it is
    FOR, but is never itself written anywhere by this service."""

    kind: str
    capability: str
    connection_slug: str
    address: Optional[str] = None
    formula: Optional[str] = None
    text: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "kind": self.kind,
            "capability": self.capability,
            "connection_slug": self.connection_slug,
        }
        if self.address is not None:
            payload["address"] = self.address
        if self.formula is not None:
            payload["formula"] = self.formula
        if self.text is not None:
            payload["text"] = self.text
        return payload


class SheetAiOrchestratorService:
    def __init__(self, sheet_editor_service, ai_service) -> None:
        self._sheet_editor_service = sheet_editor_service
        self._ai_service = ai_service

    def run_capability(
        self,
        user_id,
        node_id,
        capability_id: str,
        *,
        address: str,
        range_text: Optional[str] = None,
        intent: str = "",
        prompt: str = "",
    ) -> Dict[str, Any]:
        if capability_id not in SHEET_CAPABILITY_IDS:
            raise OfficeAiInvalidCapabilityError(
                f"Unknown capability: {capability_id!r}"
            )
        if not (address or "").strip():
            raise OfficeAiInvalidCapabilityError("address is required")

        selection = self._sheet_editor_service.read_selection_for_ai(
            user_id, node_id, address, range_text
        )

        # Explaining/summarising is a read: any resolved access level may do
        # it (a view share included). Proposing a formula to APPLY needs
        # edit — matches ``save_cells``'s own bar, checked before that
        # route is ever reached. ``sheet_freeform`` is NOT in this
        # statically-classified set (its reply's shape is not known yet) —
        # its edit check happens AFTER the reply, below.
        if capability_id in FORMULA_PRODUCING_SHEET_CAPABILITIES:
            if selection.access not in EDIT_CAPABLE_ACCESS:
                raise OfficeShareForbiddenError(node_id)
        if not selection.document.ai_enabled:
            raise OfficeAiDisabledError(node_id)

        selection_text = self._serialize_selection(selection)
        proposed_text, connection_slug = self._ai_service.run_capability(
            user_id,
            node_id,
            capability_id,
            selection_text=selection_text,
            intent=intent,
            prompt=prompt,
        )
        proposal = self._build_proposal(
            capability_id, proposed_text, connection_slug, selection.active_address
        )
        # ``sheet_freeform`` only now reveals whether it produced a formula
        # to APPLY (needs edit, same bar as the preset formula capabilities)
        # or a read-only answer (any resolved access may have it) — the
        # provider call above already happened and was budgeted/audited;
        # only the FORBIDDEN proposal is withheld here.
        if (
            proposal.kind == PROPOSAL_KIND_FORMULA
            and capability_id not in FORMULA_PRODUCING_SHEET_CAPABILITIES
            and selection.access not in EDIT_CAPABLE_ACCESS
        ):
            raise OfficeShareForbiddenError(node_id)
        return proposal.to_dict()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_selection(selection) -> str:
        """A compact TSV table: one header line describing the active cell
        (address, its current formula if any, its current value), then one
        line per cell in the requested range — never the whole workbook.
        ``OfficeAiService`` still character-caps whatever this produces."""
        active_reference = format_cell_reference(
            selection.active_address, include_sheet=False
        )
        active_formula_text = selection.active_formula or ""
        active_value_text = _cell_value_text(selection.active_value)
        lines = [
            f"Active cell: {active_reference}",
            f"Active formula: {active_formula_text}",
            f"Active value: {active_value_text}",
            "",
            "address\tvalue",
        ]
        for cell_address, cell_value in selection.range_cells:
            reference = format_cell_reference(cell_address, include_sheet=False)
            lines.append(f"{reference}\t{_cell_value_text(cell_value)}")
        return "\n".join(lines)

    @staticmethod
    def _build_proposal(
        capability_id: str,
        proposed_text: str,
        connection_slug: str,
        active_address: CellAddress,
    ) -> SheetAiProposal:
        is_formula = capability_id in FORMULA_PRODUCING_SHEET_CAPABILITIES or (
            capability_id == CAPABILITY_SHEET_FREEFORM
            and _looks_like_a_formula(proposed_text)
        )
        if is_formula:
            return SheetAiProposal(
                kind=PROPOSAL_KIND_FORMULA,
                capability=capability_id,
                connection_slug=connection_slug,
                address=format_cell_reference(active_address, include_sheet=False),
                formula=proposed_text,
            )
        return SheetAiProposal(
            kind=PROPOSAL_KIND_TEXT,
            capability=capability_id,
            connection_slug=connection_slug,
            text=proposed_text,
        )


def _looks_like_a_formula(proposed_text: str) -> bool:
    """Freeform's reply shape is not known ahead of time (the SAME
    capability id may answer either a question or a request for a formula)
    — this is the one place that sniffs it, from the model's own reply,
    exactly the way a human typing into a cell signals "this is a formula"
    to the engine. A false positive/negative is never a silent
    misclassification: a ``text`` proposal that WAS meant as a formula is
    just discarded by the client's Accept/Discard flow, and a ``formula``
    proposal that is not actually parseable becomes a ``#NAME?``/400 on
    apply via ``save_cells``, same as ``sheet_write_formula`` today."""
    return proposed_text.strip().startswith("=")


def _cell_value_text(value) -> str:
    if is_error(value):
        return str(value)
    return coerce_to_text(value)
