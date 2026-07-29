"""S147-4 unit coverage for ``OfficeSheetEditorService`` — the Sheet-specific
logic it adds ON TOP of the reused ``OfficeDocumentService``/
``AccessResolver`` (S147-1/-2): stale-version rejection, the
view-only-cannot-save rule, partial-recalc deltas-only responses
(requirement #5), and the ceiling (requirement #4). Against fakes/stubs — no
DB, no network.
"""
import json
import uuid

import pytest

from plugins.office.office.services.access_resolver import ACCESS_OWNER
from plugins.office.office.services.edit_lease_service import EditLeaseService
from plugins.office.office.services.exceptions import (
    OfficeDocLockedError,
    OfficeDocStaleVersionError,
    OfficeSheetCeilingExceededError,
    OfficeShareForbiddenError,
)
from plugins.office.office.services.sheet_content import empty_workbook_model
from plugins.office.office.services.sheet_editor_service import OfficeSheetEditorService


class _Node:
    def __init__(self, node_id, name="Budget", kind="document"):
        self.id = node_id
        self.name = name
        self.kind = kind

    def to_dict(self):
        return {"id": str(self.id), "name": self.name}


class _Document:
    def __init__(self, doc_type="sheet", current_version_id=None):
        self.doc_type = doc_type
        self.current_version_id = current_version_id

    def to_dict(self):
        return {"doc_type": self.doc_type}


class _Version:
    def __init__(self, version_id, version_no):
        self.id = version_id
        self.version_no = version_no


class FakeNodeRepository:
    def __init__(self, nodes):
        self._nodes = nodes

    def find_by_id(self, node_id):
        return self._nodes.get(node_id)


class FakeDocumentRepository:
    def __init__(self, documents_by_node_id):
        self._documents = documents_by_node_id

    def find_by_node_id(self, node_id):
        return self._documents.get(node_id)

    def add(self, document):
        return document


class FakeVersionRepository:
    def __init__(self, versions_by_id):
        self._versions = versions_by_id

    def find_by_id(self, version_id):
        return self._versions.get(version_id)


class FakeAccessResolver:
    def __init__(self, access_by_user):
        self._access_by_user = access_by_user

    def resolve(self, node_id, *, user_id=None, share_token=None):
        return self._access_by_user.get(user_id)


class FakeDocumentService:
    def __init__(self, model=None):
        self.model = model if model is not None else empty_workbook_model()
        self.saved_versions = []
        self._next_version_no = 2

    def get_content_for_node(self, node, document, version_no=None):
        version = _Version(uuid.uuid4(), 1)
        return node, document, version, json.dumps(self.model).encode("utf-8")

    def add_version_for_node(
        self, node, document, data, *, actor_user_id=None, **kwargs
    ):
        self.model = json.loads(data.decode("utf-8"))
        version = _Version(uuid.uuid4(), self._next_version_no)
        self._next_version_no += 1
        self.saved_versions.append((node, document, data, actor_user_id))
        return version


class _FakeLeaseRepository:
    def __init__(self):
        self._leases = {}

    def find_by_node_id(self, node_id):
        return self._leases.get(node_id)

    def upsert(self, node_id, holder_user_id, acquired_at, expires_at):
        class _Row:
            pass

        row = self._leases.setdefault(node_id, _Row())
        row.holder_user_id = holder_user_id
        row.acquired_at = acquired_at
        row.expires_at = expires_at
        return row

    def delete(self, node_id):
        self._leases.pop(node_id, None)


class _FakePdfService:
    """Stands in for the CORE ``pdf_service``. Records what the sheet service
    asked it to render, so a test can assert the delegation contract instead of
    re-testing core's PDF machinery."""

    def __init__(self) -> None:
        self.registered_paths = []
        self.rendered = []

    def register_plugin_template_path(self, path) -> None:
        self.registered_paths.append(path)

    def render(self, template_name, context) -> bytes:
        self.rendered.append((template_name, context))
        return b"%PDF-1.4 fake"


def _build_service(
    access_by_user, document, model=None, max_rows=100_000, max_columns=1_000
):
    node_id = uuid.uuid4()
    node = _Node(node_id)
    node_repository = FakeNodeRepository({node_id: node})
    document_repository = FakeDocumentRepository({node_id: document})
    version_repository = FakeVersionRepository({})
    access_resolver = FakeAccessResolver(access_by_user)
    document_service = FakeDocumentService(model)
    edit_lease_service = EditLeaseService(_FakeLeaseRepository())

    service = OfficeSheetEditorService(
        node_repository=node_repository,
        document_repository=document_repository,
        version_repository=version_repository,
        document_service=document_service,
        access_resolver=access_resolver,
        edit_lease_service=edit_lease_service,
        max_rows=max_rows,
        max_columns=max_columns,
        pdf_service=_FakePdfService(),
    )
    return service, node_id, document_service


def test_open_sheet_returns_the_empty_workbook_and_lease_state():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service({owner_id: ACCESS_OWNER}, document)

    view = service.open_sheet(owner_id, node_id)

    assert view.workbook_model == empty_workbook_model()
    assert view.access == ACCESS_OWNER
    assert view.lease.held is False


def test_save_cells_with_a_formula_recalculates_and_returns_only_the_delta():
    owner_id = uuid.uuid4()
    model = {
        "sheets": [{"name": "Sheet1", "cells": {"A1": {"v": 2.0}, "B1": {"v": 3.0}}}],
        "active_sheet": "Sheet1",
    }
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, document_service = _build_service(
        {owner_id: ACCESS_OWNER}, document, model=model
    )

    result = service.save_cells(
        owner_id,
        node_id,
        [{"sheet": "Sheet1", "address": "C1", "formula": "=A1+B1"}],
        base_version_no=0,
    )

    assert result.version_no == 2
    assert result.changes == {"Sheet1!C1": 5.0}
    # ONLY the changed cell is returned — A1/B1 are untouched, not re-sent.
    assert "Sheet1!A1" not in result.changes
    assert len(document_service.saved_versions) == 1


def test_save_cells_recalculates_only_the_dirty_subgraph():
    owner_id = uuid.uuid4()
    model = {
        "sheets": [
            {
                "name": "Sheet1",
                "cells": {
                    "A1": {"v": 1.0},
                    "B1": {"f": "=A1+1", "v": 2.0},
                    "C1": {"v": 99.0},  # unrelated cell — must not recalc
                },
            }
        ],
        "active_sheet": "Sheet1",
    }
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service(
        {owner_id: ACCESS_OWNER}, document, model=model
    )

    result = service.save_cells(
        owner_id,
        node_id,
        [{"sheet": "Sheet1", "address": "A1", "value": 10}],
        base_version_no=0,
    )

    assert result.changes == {"Sheet1!A1": 10.0, "Sheet1!B1": 11.0}
    assert "Sheet1!C1" not in result.changes


def test_save_with_a_stale_base_version_no_raises_and_writes_nothing():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=None)
    service, node_id, document_service = _build_service(
        {owner_id: ACCESS_OWNER}, document
    )

    with pytest.raises(OfficeDocStaleVersionError):
        service.save_cells(
            owner_id,
            node_id,
            [{"sheet": "Sheet1", "address": "A1", "value": 1}],
            base_version_no=999,
        )
    assert document_service.saved_versions == []


def test_view_only_access_cannot_save_cells():
    owner_id, viewer_id = uuid.uuid4(), uuid.uuid4()
    document = _Document(current_version_id=None)
    service, node_id, document_service = _build_service(
        {owner_id: ACCESS_OWNER, viewer_id: "view"}, document
    )

    with pytest.raises(OfficeShareForbiddenError):
        service.save_cells(
            viewer_id,
            node_id,
            [{"sheet": "Sheet1", "address": "A1", "value": 1}],
            base_version_no=0,
        )
    assert document_service.saved_versions == []


def test_save_while_locked_by_another_editor_raises():
    owner_id, editor_id = uuid.uuid4(), uuid.uuid4()
    document = _Document(current_version_id=None)
    service, node_id, document_service = _build_service(
        {owner_id: ACCESS_OWNER, editor_id: "edit"}, document
    )
    service._edit_lease_service.acquire(node_id, owner_id)

    with pytest.raises(OfficeDocLockedError):
        service.save_cells(
            editor_id,
            node_id,
            [{"sheet": "Sheet1", "address": "A1", "value": 1}],
            base_version_no=0,
        )
    assert document_service.saved_versions == []


def test_a_cell_reference_beyond_the_ceiling_raises_cleanly():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=None)
    service, node_id, _doc_service = _build_service(
        {owner_id: ACCESS_OWNER}, document, max_rows=10, max_columns=5
    )

    with pytest.raises(OfficeSheetCeilingExceededError):
        service.save_cells(
            owner_id,
            node_id,
            [{"sheet": "Sheet1", "address": "A11", "value": 1}],
            base_version_no=0,
        )


def test_recalc_forces_a_full_recalculation_and_persists_it():
    owner_id = uuid.uuid4()
    model = {
        "sheets": [{"name": "Sheet1", "cells": {"A1": {"f": "=1+1", "v": 999.0}}}],
        "active_sheet": "Sheet1",
    }
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, document_service = _build_service(
        {owner_id: ACCESS_OWNER}, document, model=model
    )

    result = service.recalc(owner_id, node_id)

    assert result.changes == {"Sheet1!A1": 2.0}  # stale cache (999) corrected
    assert len(document_service.saved_versions) == 1


# ---------------------------------------------------------------------------
# S147-4 (drag/toolbar slice) — cell styles, merges, the fill handle
# (reference-translated copy), and the PDF export format.
# ---------------------------------------------------------------------------


def test_save_cells_with_a_style_change_persists_and_survives_a_reopen():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service({owner_id: ACCESS_OWNER}, document)

    service.save_cells(
        owner_id,
        node_id,
        [
            {
                "sheet": "Sheet1",
                "address": "A1",
                "style": {"bold": True, "format": "currency"},
            }
        ],
        base_version_no=0,
    )

    reopened = service.open_sheet(owner_id, node_id)
    assert reopened.workbook_model["sheets"][0]["styles"] == {
        "A1": {"bold": True, "format": "currency"}
    }
    # The stored VALUE is untouched by formatting — the engine keeps
    # computing on the real number, never a formatted string.
    assert "A1" not in reopened.workbook_model["sheets"][0]["cells"]


def test_save_cells_style_null_clears_a_previously_set_style():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service({owner_id: ACCESS_OWNER}, document)
    service.save_cells(
        owner_id,
        node_id,
        [{"sheet": "Sheet1", "address": "A1", "style": {"bold": True}}],
        base_version_no=0,
    )

    service.save_cells(
        owner_id,
        node_id,
        [{"sheet": "Sheet1", "address": "A1", "style": None}],
        base_version_no=0,
    )

    reopened = service.open_sheet(owner_id, node_id)
    assert reopened.workbook_model["sheets"][0].get("styles", {}) == {}


def test_save_cells_an_invalid_style_raises_and_writes_nothing():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, document_service = _build_service(
        {owner_id: ACCESS_OWNER}, document
    )

    from plugins.office.office.services.exceptions import OfficeSheetContentInvalidError

    with pytest.raises(OfficeSheetContentInvalidError):
        service.save_cells(
            owner_id,
            node_id,
            [{"sheet": "Sheet1", "address": "A1", "style": {"format": "scientific"}}],
            base_version_no=0,
        )
    assert document_service.saved_versions == []


def test_save_cells_merge_persists_and_survives_a_reopen():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service({owner_id: ACCESS_OWNER}, document)

    service.save_cells(
        owner_id,
        node_id,
        [{"sheet": "Sheet1", "merge": "A1:C1"}],
        base_version_no=0,
    )

    reopened = service.open_sheet(owner_id, node_id)
    assert reopened.workbook_model["sheets"][0]["merges"] == ["A1:C1"]


def test_save_cells_merge_replaces_an_overlapping_existing_merge():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service({owner_id: ACCESS_OWNER}, document)
    service.save_cells(
        owner_id, node_id, [{"sheet": "Sheet1", "merge": "A1:B1"}], base_version_no=0
    )

    service.save_cells(
        owner_id, node_id, [{"sheet": "Sheet1", "merge": "B1:C2"}], base_version_no=0
    )

    reopened = service.open_sheet(owner_id, node_id)
    assert reopened.workbook_model["sheets"][0]["merges"] == ["B1:C2"]


def test_save_cells_unmerge_removes_the_covering_merge():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service({owner_id: ACCESS_OWNER}, document)
    service.save_cells(
        owner_id, node_id, [{"sheet": "Sheet1", "merge": "A1:C1"}], base_version_no=0
    )

    service.save_cells(
        owner_id, node_id, [{"sheet": "Sheet1", "unmerge": "B1"}], base_version_no=0
    )

    reopened = service.open_sheet(owner_id, node_id)
    assert reopened.workbook_model["sheets"][0].get("merges", []) == []


def test_recalc_preserves_styles_and_merges():
    owner_id = uuid.uuid4()
    model = {
        "sheets": [
            {
                "name": "Sheet1",
                "cells": {"A1": {"f": "=1+1", "v": 999.0}},
                "styles": {"A1": {"bold": True}},
                "merges": ["A1:B1"],
            }
        ],
        "active_sheet": "Sheet1",
    }
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service(
        {owner_id: ACCESS_OWNER}, document, model=model
    )

    service.recalc(owner_id, node_id)

    reopened = service.open_sheet(owner_id, node_id)
    assert reopened.workbook_model["sheets"][0]["styles"] == {"A1": {"bold": True}}
    assert reopened.workbook_model["sheets"][0]["merges"] == ["A1:B1"]


def test_save_cells_fill_from_translates_relative_references():
    owner_id = uuid.uuid4()
    model = {
        "sheets": [
            {
                "name": "Sheet1",
                "cells": {
                    "A1": {"v": 1.0},
                    "A2": {"v": 2.0},
                    "B1": {"f": "=A1*2", "v": 2.0},
                },
            }
        ],
        "active_sheet": "Sheet1",
    }
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service(
        {owner_id: ACCESS_OWNER}, document, model=model
    )

    result = service.save_cells(
        owner_id,
        node_id,
        [{"sheet": "Sheet1", "address": "B2", "fill_from": "B1"}],
        base_version_no=0,
    )

    assert result.changes["Sheet1!B2"] == 4.0  # =A2*2 after translation
    reopened = service.open_sheet(owner_id, node_id)
    assert reopened.workbook_model["sheets"][0]["cells"]["B2"]["f"] == "=(A2*2)"


def test_save_cells_fill_from_keeps_absolute_references_fixed():
    owner_id = uuid.uuid4()
    model = {
        "sheets": [
            {
                "name": "Sheet1",
                "cells": {
                    "A1": {"v": 10.0},
                    "B1": {"f": "=$A$1+1", "v": 11.0},
                },
            }
        ],
        "active_sheet": "Sheet1",
    }
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service(
        {owner_id: ACCESS_OWNER}, document, model=model
    )

    result = service.save_cells(
        owner_id,
        node_id,
        [{"sheet": "Sheet1", "address": "B2", "fill_from": "B1"}],
        base_version_no=0,
    )

    assert result.changes["Sheet1!B2"] == 11.0  # $A$1 stayed fixed


def test_save_cells_fill_from_copies_a_literal_value_without_translation():
    owner_id = uuid.uuid4()
    model = {
        "sheets": [{"name": "Sheet1", "cells": {"A1": {"v": "hello"}}}],
        "active_sheet": "Sheet1",
    }
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service(
        {owner_id: ACCESS_OWNER}, document, model=model
    )

    result = service.save_cells(
        owner_id,
        node_id,
        [{"sheet": "Sheet1", "address": "A2", "fill_from": "A1"}],
        base_version_no=0,
    )

    assert result.changes["Sheet1!A2"] == "hello"


def test_save_cells_fill_from_also_copies_the_source_cells_style():
    owner_id = uuid.uuid4()
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service({owner_id: ACCESS_OWNER}, document)
    service.save_cells(
        owner_id,
        node_id,
        [
            {"sheet": "Sheet1", "address": "A1", "value": 5},
            {"sheet": "Sheet1", "address": "A1", "style": {"bold": True}},
        ],
        base_version_no=0,
    )

    service.save_cells(
        owner_id,
        node_id,
        [{"sheet": "Sheet1", "address": "A2", "fill_from": "A1"}],
        base_version_no=0,
    )

    reopened = service.open_sheet(owner_id, node_id)
    assert reopened.workbook_model["sheets"][0]["styles"]["A2"] == {"bold": True}


def test_export_workbook_pdf_format_returns_pdf_bytes():
    owner_id = uuid.uuid4()
    model = {
        "sheets": [{"name": "Sheet1", "cells": {"A1": {"f": "=2+2", "v": 0.0}}}],
        "active_sheet": "Sheet1",
    }
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service(
        {owner_id: ACCESS_OWNER}, document, model=model
    )

    data, mimetype, filename = service.export_workbook(owner_id, node_id, "pdf")

    assert data.startswith(b"%PDF")
    assert mimetype == "application/pdf"
    # Delegation, not a second PDF engine: the sheet service hands core's
    # pdf_service an HTML body and the sheet template, exactly as VBWD Docs
    # does. It also renders the COMPUTED value (4), never the formula source.
    template_name, context = service._pdf_service.rendered[-1]
    assert template_name == "office_sheet.html"
    assert "4" in context["body_html"]
    assert "=2+2" not in context["body_html"]
    assert filename == "Budget.pdf"


def test_export_workbook_recalculates_before_exporting():
    owner_id = uuid.uuid4()
    model = {
        "sheets": [{"name": "Sheet1", "cells": {"A1": {"f": "=2+2", "v": 0.0}}}],
        "active_sheet": "Sheet1",
    }
    document = _Document(current_version_id=uuid.uuid4())
    service, node_id, _doc_service = _build_service(
        {owner_id: ACCESS_OWNER}, document, model=model
    )

    data, mimetype, filename = service.export_workbook(owner_id, node_id, "csv")

    assert data.decode("utf-8").strip() == "4"
    assert mimetype == "text/csv"
    assert filename == "Budget.csv"
