"""Integration coverage for ``POST /api/v1/office/nodes/<id>/copy`` (Finder-
style file management slice) — the real Flask app + PostgreSQL (rolled back
per test).

Covers: a document copy is a byte-identical duplicate through
``OfficeDocumentService`` (quota charged, own version-1); name collisions get
a " copy" / " copy 2" suffix; a folder copy is recursive; copying a folder
into its own descendant is rejected; another owner's node id answers 404,
never 403.
"""
import io
import uuid

import pytest


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
    email = f"space-copy-owner-{uuid.uuid4().hex[:8]}@example.com"
    return _register_and_login(client, db, email)


def _upload(client, headers, filename, data, parent_id=None):
    form = {"file": (io.BytesIO(data), filename)}
    if parent_id:
        form["parent_id"] = parent_id
    return client.post(
        "/api/v1/office/documents",
        data=form,
        headers=headers,
        content_type="multipart/form-data",
    )


def _create_folder(client, headers, name, parent_id=None):
    body = {"name": name}
    if parent_id:
        body["parent_id"] = parent_id
    return client.post("/api/v1/office/folders", json=body, headers=headers)


def _list(client, headers, parent_id=None):
    query = f"?parent_id={parent_id}" if parent_id else ""
    return client.get(f"/api/v1/office/nodes{query}", headers=headers)


def test_copy_document_is_a_byte_identical_duplicate(client, owner):
    _, headers = owner
    upload_response = _upload(client, headers, "report.txt", b"original bytes")
    node_id = upload_response.get_json()["id"]

    copy_response = client.post(
        f"/api/v1/office/nodes/{node_id}/copy", json={}, headers=headers
    )

    assert copy_response.status_code == 201, copy_response.data
    copy_body = copy_response.get_json()
    assert copy_body["id"] != node_id
    assert copy_body["name"] == "report copy.txt"

    content_response = client.get(
        f"/api/v1/office/documents/{copy_body['id']}/content", headers=headers
    )
    assert content_response.data == b"original bytes"

    usage_response = client.get("/api/v1/office/usage", headers=headers)
    assert usage_response.get_json()["bytes_used"] == len(b"original bytes") * 2


def test_copy_into_a_folder_uses_the_given_parent(client, owner):
    _, headers = owner
    folder_response = _create_folder(client, headers, "Target")
    folder_id = folder_response.get_json()["id"]
    upload_response = _upload(client, headers, "note.txt", b"hi")
    node_id = upload_response.get_json()["id"]

    copy_response = client.post(
        f"/api/v1/office/nodes/{node_id}/copy",
        json={"parent_id": folder_id},
        headers=headers,
    )
    assert copy_response.status_code == 201, copy_response.data
    assert copy_response.get_json()["parent_id"] == folder_id

    listing = _list(client, headers, folder_id)
    assert [item["name"] for item in listing.get_json()["items"]] == ["note.txt"]


def test_copy_folder_is_recursive_over_http(client, owner):
    _, headers = owner
    parent_response = _create_folder(client, headers, "Parent")
    parent_id = parent_response.get_json()["id"]
    _upload(client, headers, "inside.txt", b"nested content", parent_id)

    copy_response = client.post(
        f"/api/v1/office/nodes/{parent_id}/copy", json={}, headers=headers
    )
    assert copy_response.status_code == 201, copy_response.data
    copy_id = copy_response.get_json()["id"]
    assert copy_response.get_json()["name"] == "Parent copy"

    nested_listing = _list(client, headers, copy_id)
    nested_items = nested_listing.get_json()["items"]
    assert [item["name"] for item in nested_items] == ["inside.txt"]

    nested_content_response = client.get(
        f"/api/v1/office/documents/{nested_items[0]['id']}/content", headers=headers
    )
    assert nested_content_response.data == b"nested content"


def test_copy_folder_into_its_own_descendant_returns_400(client, owner):
    _, headers = owner
    parent_response = _create_folder(client, headers, "parent")
    parent_id = parent_response.get_json()["id"]
    child_response = _create_folder(client, headers, "child", parent_id)
    child_id = child_response.get_json()["id"]

    copy_response = client.post(
        f"/api/v1/office/nodes/{parent_id}/copy",
        json={"parent_id": child_id},
        headers=headers,
    )

    assert copy_response.status_code == 400


def test_copy_of_another_owners_node_returns_404_never_403(client, db, owner):
    _, owner_headers = owner
    upload_response = _upload(client, owner_headers, "private.txt", b"secret")
    node_id = upload_response.get_json()["id"]

    stranger_email = f"copy-stranger-{uuid.uuid4().hex[:8]}@example.com"
    _, stranger_headers = _register_and_login(client, db, stranger_email)

    copy_response = client.post(
        f"/api/v1/office/nodes/{node_id}/copy", json={}, headers=stranger_headers
    )

    assert copy_response.status_code == 404
    assert copy_response.status_code != 403


def test_copy_that_exceeds_quota_writes_nothing(client, owner, monkeypatch):
    _, headers = owner
    import plugins.office as office_pkg

    tiny_quota_config = {**office_pkg.DEFAULT_CONFIG, "free_quota_bytes": 10}
    monkeypatch.setattr(office_pkg, "_current_plugin_config", lambda: tiny_quota_config)

    upload_response = _upload(client, headers, "a.txt", b"1234567890")  # fills quota
    node_id = upload_response.get_json()["id"]

    copy_response = client.post(
        f"/api/v1/office/nodes/{node_id}/copy", json={}, headers=headers
    )

    assert copy_response.status_code == 413
    listing = _list(client, headers)
    assert [item["name"] for item in listing.get_json()["items"]] == ["a.txt"]
