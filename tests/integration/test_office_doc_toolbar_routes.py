"""S147-3 toolbar bundle integration coverage — image assets, "insert table
from file", and export — against the real Flask app + PostgreSQL (rolled
back per test). Written specifically because the unit tests for these
services use lightweight fakes that do not model
``OfficeNodeAccess.resolve_parent_folder``'s real "no trashed parent" rule —
which is exactly what regresses here if the hidden ``_assets`` folder's
upload path ever stops accounting for it.
"""
import io
import uuid

import pytest

EMPTY_CONTENT = {"type": "doc", "content": [{"type": "paragraph"}]}


@pytest.fixture
def client(app):
    return app.test_client()


def _seed_office_use_permission(db):
    from vbwd.services.rbac_seeder import seed_default_rbac

    from plugins.office.office.services.access_seeder import grant_use_permission

    seed_default_rbac(db.session)
    grant_use_permission(db.session)
    db.session.commit()


def _login_and_get_token(client, email: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    body = response.get_json()
    return body.get("token") or body.get("access_token")


def _register_and_login(client, db, email):
    register_response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "StrongPass123!"}
    )
    assert register_response.status_code in (200, 201), register_response.data

    from vbwd.models.user import User

    user = db.session.query(User).filter_by(email=email).first()
    user.status = "ACTIVE"
    db.session.commit()

    token = _login_and_get_token(client, email, "StrongPass123!")
    assert token, f"login did not return a token: {register_response.data}"
    return user, {"Authorization": f"Bearer {token}"}


@pytest.fixture
def owner(client, db):
    _seed_office_use_permission(db)
    email = f"doc-toolbar-owner-{uuid.uuid4().hex[:8]}@example.com"
    return _register_and_login(client, db, email)


def _create_doc(client, headers, name="Untitled"):
    return client.post("/api/v1/office/docs", json={"name": name}, headers=headers)


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _upload_asset(client, headers, node_id, filename, data):
    return client.post(
        f"/api/v1/office/docs/{node_id}/assets",
        data={"file": (io.BytesIO(data), filename)},
        headers=headers,
        content_type="multipart/form-data",
    )


# ---------------------------------------------------------------------------
# Insert image — upload, then read back, then a SECOND upload for the same
# owner (proving the hidden folder is reused correctly across requests).
# ---------------------------------------------------------------------------


def test_upload_asset_returns_201_with_an_office_asset_src(client, owner):
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]

    response = _upload_asset(client, headers, node_id, "cat.png", PNG_BYTES)

    assert response.status_code == 201, response.data
    body = response.get_json()
    assert body["src"] == f"office-asset:{body['node_id']}"


def test_a_second_asset_upload_for_the_same_owner_also_succeeds(client, owner):
    """Regression: the hidden `_assets` folder must stay usable as an
    upload target on every subsequent request, not just a lucky first one."""
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]

    first = _upload_asset(client, headers, node_id, "one.png", PNG_BYTES)
    second = _upload_asset(client, headers, node_id, "two.png", PNG_BYTES)

    assert first.status_code == 201, first.data
    assert second.status_code == 201, second.data
    assert first.get_json()["node_id"] != second.get_json()["node_id"]


def test_get_asset_streams_the_uploaded_bytes_once_referenced_in_content(client, owner):
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]
    uploaded = _upload_asset(client, headers, node_id, "cat.png", PNG_BYTES).get_json()

    save_response = client.put(
        f"/api/v1/office/docs/{node_id}",
        json={
            "content": {
                "type": "doc",
                "content": [{"type": "image", "attrs": {"src": uploaded["src"]}}],
            },
            "base_version_no": 1,
        },
        headers=headers,
    )
    assert save_response.status_code == 200, save_response.data

    get_response = client.get(
        f"/api/v1/office/docs/{node_id}/assets/{uploaded['node_id']}", headers=headers
    )
    assert get_response.status_code == 200
    assert get_response.data == PNG_BYTES


def test_get_asset_404s_when_not_referenced_by_the_documents_own_content(client, owner):
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]
    uploaded = _upload_asset(client, headers, node_id, "cat.png", PNG_BYTES).get_json()
    # Content was never updated to reference the uploaded asset.

    get_response = client.get(
        f"/api/v1/office/docs/{node_id}/assets/{uploaded['node_id']}", headers=headers
    )
    assert get_response.status_code == 404


# ---------------------------------------------------------------------------
# Insert table from file — real .csv, then real .xlsx.
# ---------------------------------------------------------------------------


def test_table_import_from_a_real_csv_upload(client, owner):
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]
    csv_bytes = b"Name,Age\nAda,36\n"

    response = client.post(
        f"/api/v1/office/docs/{node_id}/table-import",
        data={"file": (io.BytesIO(csv_bytes), "people.csv")},
        headers=headers,
        content_type="multipart/form-data",
    )

    assert response.status_code == 200, response.data
    body = response.get_json()
    assert body["rows"] == [["Name", "Age"], ["Ada", "36"]]
    assert body["row_count"] == 2
    assert body["column_count"] == 2


def test_table_import_413s_over_the_configured_column_ceiling(client, owner):
    """The default ``doc_table_import_max_columns`` is 50
    (``plugins/office/__init__.py``'s ``DEFAULT_CONFIG``) — a genuinely
    51-column row must be refused with a clean 413, not an unbounded read."""
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]
    too_wide_csv = (",".join(str(n) for n in range(51)) + "\n").encode("utf-8")

    response = client.post(
        f"/api/v1/office/docs/{node_id}/table-import",
        data={"file": (io.BytesIO(too_wide_csv), "wide.csv")},
        headers=headers,
        content_type="multipart/form-data",
    )

    assert response.status_code == 413, response.data
    assert "error" in response.get_json()


# ---------------------------------------------------------------------------
# Export — PDF/DOCX/Markdown all return real file bytes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "export_format,expected_prefix",
    [("md", b"Hello Docs"), ("pdf", b"%PDF-"), ("docx", b"PK")],
)
def test_export_returns_real_file_bytes(client, owner, export_format, expected_prefix):
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]
    save_response = client.put(
        f"/api/v1/office/docs/{node_id}",
        json={
            "content": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Hello Docs"}],
                    }
                ],
            },
            "base_version_no": 1,
        },
        headers=headers,
    )
    assert save_response.status_code == 200, save_response.data

    response = client.post(
        f"/api/v1/office/docs/{node_id}/export?format={export_format}", headers=headers
    )

    assert response.status_code == 200, response.data
    assert response.data.startswith(expected_prefix)
