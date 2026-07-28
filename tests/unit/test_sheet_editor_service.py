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
