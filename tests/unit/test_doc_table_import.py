"""S147-3 toolbar bundle unit coverage for the Docs "insert table from file"
action — ``parse_table_from_upload`` (pure parsing, reuses
``sheet_import_export.py``'s parser VERBATIM per the module's own docstring)
and ``OfficeDocTableImportService`` (the access-checked DI wrapper). No DB.
"""
import uuid

import pytest

from plugins.office.office.models.office_document import DOC_TYPE_TEXT
from plugins.office.office.models.office_node import NODE_KIND_DOCUMENT
from plugins.office.office.models.office_share import PERMISSION_VIEW
from plugins.office.office.services.access_resolver import ACCESS_OWNER
from plugins.office.office.services.doc_table_import import (
    OfficeDocTableImportService,
    parse_table_from_upload,
)
from plugins.office.office.services.exceptions import (
    OfficeDocTableImportFormatError,
    OfficeShareForbiddenError,
)
from plugins.office.office.services.sheet_content import SheetCeiling
from plugins.office.office.services.sheet_import_export import export_xlsx, import_csv

GENEROUS_CEILING = SheetCeiling(max_rows=100_000, max_columns=1_000)


# ---------------------------------------------------------------------------
# parse_table_from_upload — pure parsing.
# ---------------------------------------------------------------------------


def test_parses_a_csv_upload_into_rows_of_strings():
    data = b"Name,Age\nAda,36\nGrace,85\n"
    table = parse_table_from_upload(data, "csv", max_rows=100, max_columns=10)

    assert table.rows == [["Name", "Age"], ["Ada", "36"], ["Grace", "85"]]
    assert table.to_dict() == {
        "rows": [["Name", "Age"], ["Ada", "36"], ["Grace", "85"]],
        "row_count": 3,
        "column_count": 2,
    }


def test_parses_an_xlsx_upload_into_rows_of_strings():
    csv_workbook = import_csv(b"1,2\n3,4\n", GENEROUS_CEILING).workbook
    xlsx_bytes = export_xlsx(csv_workbook)

    table = parse_table_from_upload(xlsx_bytes, "xlsx", max_rows=100, max_columns=10)

    assert table.rows == [["1", "2"], ["3", "4"]]


def test_a_formula_cell_is_recalculated_to_its_display_text_not_kept_as_a_formula():
    """A Doc table cell has no formula concept (doc_content.py's schema) —
    every formula cell is reduced to its computed value before insertion."""
    workbook = import_csv(b"2,3\n", GENEROUS_CEILING).workbook
    from plugins.office.office.sheet.cell import parse_cell_reference

    workbook.set_formula(parse_cell_reference("C1", default_sheet="Sheet1"), "=A1+B1")
    xlsx_bytes = export_xlsx(workbook)

    table = parse_table_from_upload(xlsx_bytes, "xlsx", max_rows=100, max_columns=10)

    assert table.rows == [["2", "3", "5"]]


def test_rejects_an_unsupported_import_format():
    with pytest.raises(OfficeDocTableImportFormatError):
        parse_table_from_upload(b"1,2\n", "pdf", max_rows=100, max_columns=10)


def test_an_oversized_upload_is_capped_not_truncated():
    from plugins.office.office.services.sheet_content import SheetCeiling as _Ceiling
    from plugins.office.office.services.exceptions import (
        OfficeSheetCeilingExceededError,
    )

    tiny_ceiling = _Ceiling(max_rows=100, max_columns=2)
    with pytest.raises(OfficeSheetCeilingExceededError):
        parse_table_from_upload(
            b"1,2,3\n",
            "csv",
            max_rows=tiny_ceiling.max_rows,
            max_columns=tiny_ceiling.max_columns,
        )


def test_an_empty_upload_parses_to_no_rows():
    table = parse_table_from_upload(b"", "csv", max_rows=100, max_columns=10)
    assert table.rows == []
    assert table.to_dict() == {"rows": [], "row_count": 0, "column_count": 0}


# ---------------------------------------------------------------------------
# OfficeDocTableImportService — the access-checked wrapper.
# ---------------------------------------------------------------------------


class _Node:
    def __init__(self, node_id, kind=NODE_KIND_DOCUMENT):
        self.id = node_id
        self.kind = kind


class _Document:
    def __init__(self, doc_type=DOC_TYPE_TEXT):
        self.doc_type = doc_type


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


class FakeAccessResolver:
    def __init__(self, access_by_user):
        self._access_by_user = access_by_user

    def resolve(self, node_id, *, user_id=None, share_token=None):
        return self._access_by_user.get(user_id)


def _build_service(access_by_user):
    node_id = uuid.uuid4()
    node = _Node(node_id)
    document = _Document()
    service = OfficeDocTableImportService(
        node_repository=FakeNodeRepository({node_id: node}),
        document_repository=FakeDocumentRepository({node_id: document}),
        access_resolver=FakeAccessResolver(access_by_user),
        max_rows=1000,
        max_columns=100,
    )
    return service, node_id


def test_import_table_succeeds_for_an_edit_capable_user():
    owner_id = uuid.uuid4()
    service, node_id = _build_service({owner_id: ACCESS_OWNER})

    table = service.import_table(owner_id, node_id, b"a,b\n1,2\n", "csv")

    assert table.rows == [["a", "b"], ["1", "2"]]


def test_import_table_is_forbidden_for_a_view_only_share():
    owner_id = uuid.uuid4()
    viewer_id = uuid.uuid4()
    service, node_id = _build_service(
        {owner_id: ACCESS_OWNER, viewer_id: PERMISSION_VIEW}
    )

    with pytest.raises(OfficeShareForbiddenError):
        service.import_table(viewer_id, node_id, b"a,b\n", "csv")
