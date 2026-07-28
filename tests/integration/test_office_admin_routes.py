"""Admin catalogue + storage-overview routes (S147 gap-fix) — the real Flask
app + PostgreSQL (rolled back per test). Covers:

* the paginate() envelope shape on ``GET /api/v1/admin/office/documents``
* the storage overview shape on ``GET /api/v1/admin/office/storage``
* GDPR: no content/storage-key/token field ever appears in the response
* both routes 401/403 without admin auth + the ``office.documents.view``
  permission
"""
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from vbwd.models.enums import UserRole, UserStatus
from vbwd.models.user import User

HEADERS = {"Authorization": "Bearer valid"}


@pytest.fixture
def client(app):
    return app.test_client()


def _make_admin(db):
    admin = User(
        id=uuid4(),
        email=f"office-admin-{uuid4().hex[:8]}@example.com",
        password_hash="x",
        status=UserStatus.ACTIVE,
        role=UserRole.ADMIN,
    )
    db.session.add(admin)
    db.session.commit()
    return admin


def _auth_as_admin(monkeypatch, admin, *, has_permission=True):
    import vbwd.middleware.auth as auth_mod

    repo = MagicMock()
    repo.find_by_id.return_value = admin
    auth_service = MagicMock()
    auth_service.verify_token.return_value = str(admin.id)
    monkeypatch.setattr(auth_mod, "UserRepository", lambda *a, **k: repo)
    monkeypatch.setattr(auth_mod, "AuthService", lambda *a, **k: auth_service)
    monkeypatch.setattr(type(admin), "is_admin", property(lambda self: True))
    monkeypatch.setattr(
        type(admin), "has_permission", lambda self, perm: has_permission
    )


def _register_and_login(client, db, email):
    register_response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "StrongPass123!"}
    )
    assert register_response.status_code in (200, 201), register_response.data
    user = db.session.query(User).filter_by(email=email).first()
    user.status = "ACTIVE"
    db.session.commit()
    login_response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "StrongPass123!"}
    )
    body = login_response.get_json()
    token = body.get("token") or body.get("access_token")
    return user, {"Authorization": f"Bearer {token}"}


def _create_text_doc(client, headers, name):
    return client.post("/api/v1/office/docs", json={"name": name}, headers=headers)


def test_admin_documents_returns_the_paginate_envelope(client, db, monkeypatch):
    admin = _make_admin(db)
    _auth_as_admin(monkeypatch, admin)

    from plugins.office.office.services.access_seeder import grant_use_permission

    grant_use_permission(db.session)
    db.session.commit()
    _, owner_headers = _register_and_login(
        client, db, f"doc-owner-{uuid4().hex[:8]}@example.com"
    )
    create_response = _create_text_doc(client, owner_headers, "Quarterly Notes")
    assert create_response.status_code == 201, create_response.data

    response = client.get("/api/v1/admin/office/documents", headers=HEADERS)
    assert response.status_code == 200, response.data
    body = response.get_json()
    assert set(body.keys()) == {"items", "total", "page", "per_page", "pages"}
    assert body["total"] >= 1
    assert any(item["name"] == "Quarterly Notes" for item in body["items"])


def test_admin_documents_rows_never_expose_content_or_a_token(client, db, monkeypatch):
    admin = _make_admin(db)
    _auth_as_admin(monkeypatch, admin)

    from plugins.office.office.services.access_seeder import grant_use_permission

    grant_use_permission(db.session)
    db.session.commit()
    _, owner_headers = _register_and_login(
        client, db, f"doc-owner-{uuid4().hex[:8]}@example.com"
    )
    _create_text_doc(client, owner_headers, "Private Plan")

    response = client.get("/api/v1/admin/office/documents", headers=HEADERS)
    body = response.get_json()
    forbidden_keys = {
        "content",
        "storage_key",
        "sealed_data_key",
        "share_token",
        "token",
    }
    for item in body["items"]:
        assert not (forbidden_keys & set(item.keys()))
        assert "owner" in item and set(item["owner"].keys()) == {"id", "email"}


def test_admin_storage_overview_shape(client, db, monkeypatch):
    admin = _make_admin(db)
    _auth_as_admin(monkeypatch, admin)

    from plugins.office.office.services.access_seeder import grant_use_permission

    grant_use_permission(db.session)
    db.session.commit()
    _, owner_headers = _register_and_login(
        client, db, f"doc-owner-{uuid4().hex[:8]}@example.com"
    )
    _create_text_doc(client, owner_headers, "Some Doc")

    response = client.get("/api/v1/admin/office/storage", headers=HEADERS)
    assert response.status_code == 200, response.data
    body = response.get_json()
    assert "total_bytes" in body
    assert "per_user" in body
    assert body["total_bytes"] >= 0
    for entry in body["per_user"]:
        assert set(entry.keys()) == {"user_id", "email", "bytes_used"}


def test_admin_documents_requires_admin_auth(client):
    response = client.get("/api/v1/admin/office/documents")
    assert response.status_code == 401


def test_admin_documents_requires_the_documents_view_permission(
    client, db, monkeypatch
):
    admin = _make_admin(db)
    _auth_as_admin(monkeypatch, admin, has_permission=False)

    response = client.get("/api/v1/admin/office/documents", headers=HEADERS)
    assert response.status_code == 403


def test_admin_storage_requires_admin_auth(client):
    response = client.get("/api/v1/admin/office/storage")
    assert response.status_code == 401
