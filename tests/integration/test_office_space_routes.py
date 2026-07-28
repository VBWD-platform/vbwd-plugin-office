"""S147-1 integration coverage — the real Flask app + PostgreSQL (rolled back
per test). Covers test #10 (upload -> list -> download byte-identical round
trip), #6 (html never served inline), #4 (upload-cap 413 persists nothing),
#3 (quota 413 at the HTTP layer), and #9 (another user's node -> 404, never
403) from the S147-1 spec's test list.
"""
import hashlib
import io
import uuid

import pytest

import plugins.office as office_pkg

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


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
    email = f"space-owner-{uuid.uuid4().hex[:8]}@example.com"
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


def test_upload_list_download_round_trips_byte_identical(client, owner):
    _, headers = owner
    payload = bytes(range(256)) * 4096  # a real binary fixture, not text

    upload_response = _upload(client, headers, "archive.bin", payload)
    assert upload_response.status_code == 201, upload_response.data
    node_id = upload_response.get_json()["id"]

    list_response = client.get("/api/v1/office/nodes", headers=headers)
    assert list_response.status_code == 200
    listed_ids = [item["id"] for item in list_response.get_json()["items"]]
    assert node_id in listed_ids

    download_response = client.get(
        f"/api/v1/office/documents/{node_id}/content", headers=headers
    )
    assert download_response.status_code == 200
    assert download_response.data == payload
    assert (
        hashlib.sha256(download_response.data).hexdigest()
        == hashlib.sha256(payload).hexdigest()
    )
    assert download_response.headers["X-Content-Type-Options"] == "nosniff"


def test_html_upload_downloads_as_attachment_never_inline(client, owner):
    _, headers = owner
    html_payload = b"<!DOCTYPE html><html><body><script>alert(1)</script></body></html>"

    upload_response = _upload(client, headers, "page.html", html_payload)
    assert upload_response.status_code == 201, upload_response.data
    assert upload_response.get_json()["document"]["mime_type"] == "text/html"
    node_id = upload_response.get_json()["id"]

    download_response = client.get(
        f"/api/v1/office/documents/{node_id}/content", headers=headers
    )
    disposition = download_response.headers.get("Content-Disposition", "")
    assert disposition.startswith("attachment"), disposition
    assert download_response.headers["X-Content-Type-Options"] == "nosniff"
    # C1/D7 taken literally: a non-previewable document is not merely delivered
    # as an attachment, it stops being advertised as HTML at all. "attachment"
    # plus "nosniff" already stops a modern browser rendering it, but the stored
    # mime is attacker-influenced and this is the one control whose failure mode
    # is stored-XSS on our own origin — so it gets belt AND braces.
    assert download_response.mimetype == "application/octet-stream"


def test_png_on_the_preview_allowlist_is_served_inline(client, owner):
    _, headers = owner

    upload_response = _upload(client, headers, "photo.png", PNG_HEADER)
    assert upload_response.status_code == 201, upload_response.data
    node_id = upload_response.get_json()["id"]

    download_response = client.get(
        f"/api/v1/office/documents/{node_id}/content", headers=headers
    )
    disposition = download_response.headers.get("Content-Disposition", "")
    assert not disposition.startswith("attachment"), disposition
    assert download_response.headers["X-Content-Type-Options"] == "nosniff"


def test_upload_over_the_cap_is_rejected_and_persists_nothing(
    client, owner, monkeypatch
):
    _, headers = owner
    tiny_cap_config = {**office_pkg.DEFAULT_CONFIG, "max_upload_bytes": 8}
    monkeypatch.setattr(office_pkg, "_current_plugin_config", lambda: tiny_cap_config)

    upload_response = _upload(client, headers, "too-big.bin", b"0123456789")
    assert upload_response.status_code == 413

    list_response = client.get("/api/v1/office/nodes", headers=headers)
    assert list_response.get_json()["items"] == []

    usage_response = client.get("/api/v1/office/usage", headers=headers)
    assert usage_response.get_json()["bytes_used"] == 0


def test_upload_that_crosses_quota_is_rejected(client, owner, monkeypatch):
    _, headers = owner
    tiny_quota_config = {**office_pkg.DEFAULT_CONFIG, "free_quota_bytes": 10}
    monkeypatch.setattr(office_pkg, "_current_plugin_config", lambda: tiny_quota_config)

    first = _upload(client, headers, "a.txt", b"1234567890")  # fills the quota
    assert first.status_code == 201, first.data

    second = _upload(client, headers, "b.txt", b"1")
    assert second.status_code == 413

    usage_response = client.get("/api/v1/office/usage", headers=headers)
    usage = usage_response.get_json()
    assert usage["bytes_used"] == 10
    assert usage["bytes_quota"] == 10


def test_another_users_node_id_returns_404_not_403(client, db, owner):
    _, owner_headers = owner
    upload_response = _upload(client, owner_headers, "private.txt", b"top secret")
    node_id = upload_response.get_json()["id"]

    stranger_email = f"stranger-{uuid.uuid4().hex[:8]}@example.com"
    _, stranger_headers = _register_and_login(client, db, stranger_email)

    response = client.get(
        f"/api/v1/office/documents/{node_id}", headers=stranger_headers
    )

    assert response.status_code == 404
    assert response.status_code != 403


def test_trash_then_purge_frees_quota(client, owner):
    _, headers = owner
    upload_response = _upload(client, headers, "temp.txt", b"1234567890")
    node_id = upload_response.get_json()["id"]

    trash_response = client.delete(f"/api/v1/office/nodes/{node_id}", headers=headers)
    assert trash_response.status_code == 204

    listing_after_trash = client.get("/api/v1/office/nodes", headers=headers)
    assert listing_after_trash.get_json()["items"] == []

    usage_after_trash = client.get("/api/v1/office/usage", headers=headers)
    assert usage_after_trash.get_json()["bytes_used"] == 10  # bytes preserved

    purge_response = client.delete(
        f"/api/v1/office/nodes/{node_id}?purge=true", headers=headers
    )
    assert purge_response.status_code == 204

    usage_after_purge = client.get("/api/v1/office/usage", headers=headers)
    assert usage_after_purge.get_json()["bytes_used"] == 0


def test_restore_version_creates_a_new_version_entry(client, owner):
    _, headers = owner
    upload_response = _upload(client, headers, "notes.txt", b"first draft")
    node_id = upload_response.get_json()["id"]

    client.post(
        f"/api/v1/office/documents/{node_id}/versions",
        data={"file": (io.BytesIO(b"second draft"), "notes.txt")},
        headers=headers,
        content_type="multipart/form-data",
    )

    restore_response = client.post(
        f"/api/v1/office/documents/{node_id}/restore",
        json={"version_no": 1},
        headers=headers,
    )
    assert restore_response.status_code == 201
    assert restore_response.get_json()["version_no"] == 3

    versions_response = client.get(
        f"/api/v1/office/documents/{node_id}/versions", headers=headers
    )
    version_numbers = [v["version_no"] for v in versions_response.get_json()["items"]]
    assert version_numbers == [1, 2, 3]

    content_response = client.get(
        f"/api/v1/office/documents/{node_id}/content", headers=headers
    )
    assert content_response.data == b"first draft"
