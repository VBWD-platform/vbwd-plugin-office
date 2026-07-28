"""Unit coverage for ``OfficeAdminService`` — the fe-admin catalogue +
storage-overview read model. Against fakes (no DB)."""
from plugins.office.office.services.office_admin_service import OfficeAdminService


class FakeAdminDocumentRow:
    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return self._payload


class FakeDocumentRepository:
    def __init__(self, rows, total):
        self._rows = rows
        self._total = total
        self.calls = []

    def list_for_admin(self, page, per_page):
        self.calls.append((page, per_page))
        return self._rows, self._total


class FakeUsageRepository:
    def __init__(self, rows):
        self._rows = rows

    def list_all_with_owner_email(self):
        return self._rows


def test_list_documents_returns_dicts_and_the_total():
    rows = [FakeAdminDocumentRow({"id": "doc-1", "name": "Report"})]
    document_repository = FakeDocumentRepository(rows, total=42)
    service = OfficeAdminService(document_repository, FakeUsageRepository([]))

    items, total = service.list_documents(page=2, per_page=10)

    assert items == [{"id": "doc-1", "name": "Report"}]
    assert total == 42
    assert document_repository.calls == [(2, 10)]


def test_list_documents_never_exposes_content_fields():
    """GDPR guard: the service only ever returns whatever the repository row
    projects — assert the row's own `to_dict()` contract never smuggles a
    content/storage-key field through (this test protects the CONTRACT, the
    repository test protects the QUERY)."""
    rows = [
        FakeAdminDocumentRow(
            {
                "id": "doc-1",
                "owner": {"id": "user-1", "email": "owner@example.com"},
                "name": "Report",
                "doc_type": "text",
                "mime_type": "text/plain",
                "size_bytes": 10,
                "version_count": 2,
                "created_at": "2026-07-27T00:00:00",
                "updated_at": "2026-07-27T00:00:00",
            }
        )
    ]
    service = OfficeAdminService(
        FakeDocumentRepository(rows, total=1), FakeUsageRepository([])
    )

    items, _total = service.list_documents(page=1, per_page=20)

    forbidden_keys = {
        "content",
        "storage_key",
        "sealed_data_key",
        "share_token",
        "token",
    }
    assert not (forbidden_keys & set(items[0].keys()))


def test_storage_overview_sums_total_bytes_from_per_user_rows():
    usage_repository = FakeUsageRepository(
        [("user-1", "a@example.com", 100), ("user-2", "b@example.com", 250)]
    )
    service = OfficeAdminService(FakeDocumentRepository([], 0), usage_repository)

    overview = service.storage_overview()

    assert overview["total_bytes"] == 350
    assert overview["per_user"] == [
        {"user_id": "user-1", "email": "a@example.com", "bytes_used": 100},
        {"user_id": "user-2", "email": "b@example.com", "bytes_used": 250},
    ]


def test_storage_overview_is_empty_but_well_formed_with_no_usage():
    service = OfficeAdminService(FakeDocumentRepository([], 0), FakeUsageRepository([]))

    overview = service.storage_overview()

    assert overview == {"total_bytes": 0, "per_user": []}
