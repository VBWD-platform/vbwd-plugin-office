"""S147-3 integration coverage — the real Flask app + PostgreSQL (rolled back
per test). Covers the sprint's test list:

#1  save -> reload -> the model round-trips exactly, version count +1
#2  a stale base_version_no -> 409, no version written
#3  lease: acquire -> a second user is read-only -> lapse -> take-over
#4  a view-only share cannot save and cannot take a lease
#6  the AI route rejects an unknown capability id and a raw prompt field
#7  budget exhausted -> 429, no provider call made
#8  ai_enabled=false -> 403 regardless of permission
#9  no configured LLM connection degrades to a clear error, never a 500
#11 create -> edit -> autosave -> restore an older version
"""
import time
import uuid

import pytest

import plugins.office as office_pkg

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
    email = f"docs-owner-{uuid.uuid4().hex[:8]}@example.com"
    return _register_and_login(client, db, email)


def _create_doc(client, headers, name="Untitled"):
    return client.post("/api/v1/office/docs", json={"name": name}, headers=headers)


def _create_share(client, headers, node_id, **kwargs):
    return client.post(
        f"/api/v1/office/nodes/{node_id}/shares", json=kwargs, headers=headers
    )


def _enable_ai(client, headers, node_id, content, base_version_no):
    return client.put(
        f"/api/v1/office/docs/{node_id}",
        json={
            "content": content,
            "base_version_no": base_version_no,
            "ai_enabled": True,
        },
        headers=headers,
    )


# ---------------------------------------------------------------------------
# #1 — save -> reload round-trips exactly; version count +1
# ---------------------------------------------------------------------------


def test_create_then_save_then_reload_round_trips_the_model_exactly(client, owner):
    _, headers = owner
    create_response = _create_doc(client, headers)
    assert create_response.status_code == 201, create_response.data
    body = create_response.get_json()
    node_id = body["id"]
    assert body["content"] == EMPTY_CONTENT
    assert body["version_no"] == 1

    new_content = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello Docs"}]}
        ],
    }
    save_response = client.put(
        f"/api/v1/office/docs/{node_id}",
        json={"content": new_content, "base_version_no": 1},
        headers=headers,
    )
    assert save_response.status_code == 200, save_response.data
    assert save_response.get_json()["version_no"] == 2

    reload_response = client.get(f"/api/v1/office/docs/{node_id}", headers=headers)
    assert reload_response.status_code == 200
    reloaded = reload_response.get_json()
    assert reloaded["content"] == new_content
    assert reloaded["version_no"] == 2

    versions_response = client.get(
        f"/api/v1/office/documents/{node_id}/versions", headers=headers
    )
    assert len(versions_response.get_json()["items"]) == 2


# ---------------------------------------------------------------------------
# #2 — a stale base_version_no -> 409, nothing written
# ---------------------------------------------------------------------------


def test_stale_base_version_no_is_rejected_with_409_and_writes_nothing(client, owner):
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]

    response = client.put(
        f"/api/v1/office/docs/{node_id}",
        json={"content": EMPTY_CONTENT, "base_version_no": 999},
        headers=headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "stale_version"

    versions_response = client.get(
        f"/api/v1/office/documents/{node_id}/versions", headers=headers
    )
    assert len(versions_response.get_json()["items"]) == 1


# ---------------------------------------------------------------------------
# #3 — lease: acquire -> second user read-only -> lapse -> take-over
# ---------------------------------------------------------------------------


def test_lease_second_editor_is_read_only_until_takeover(
    client, db, owner, monkeypatch
):
    short_lease_config = {**office_pkg.DEFAULT_CONFIG, "edit_lease_seconds": 1}
    monkeypatch.setattr(
        office_pkg, "_current_plugin_config", lambda: short_lease_config
    )

    _, owner_headers = owner
    node_id = _create_doc(client, owner_headers).get_json()["id"]

    editor_email = f"docs-editor-{uuid.uuid4().hex[:8]}@example.com"
    _, editor_headers = _register_and_login(client, db, editor_email)
    _create_share(
        client,
        owner_headers,
        node_id,
        permission="edit",
        recipient_email=editor_email,
    )

    first_lease = client.post(
        f"/api/v1/office/docs/{node_id}/lease", headers=owner_headers
    )
    assert first_lease.status_code == 200
    assert first_lease.get_json()["granted"] is True

    second_lease = client.post(
        f"/api/v1/office/docs/{node_id}/lease", headers=editor_headers
    )
    assert second_lease.status_code == 200
    assert second_lease.get_json()["granted"] is False
    assert second_lease.get_json()["is_self"] is False

    time.sleep(1.1)  # let the 1-second lease lapse
    takeover = client.post(
        f"/api/v1/office/docs/{node_id}/lease", headers=editor_headers
    )
    assert takeover.status_code == 200
    assert takeover.get_json()["granted"] is True
    assert takeover.get_json()["is_self"] is True


def test_save_while_locked_by_another_editor_returns_409(client, db, owner):
    _, owner_headers = owner
    node_id = _create_doc(client, owner_headers).get_json()["id"]

    editor_email = f"docs-editor-{uuid.uuid4().hex[:8]}@example.com"
    _, editor_headers = _register_and_login(client, db, editor_email)
    _create_share(
        client, owner_headers, node_id, permission="edit", recipient_email=editor_email
    )

    client.post(f"/api/v1/office/docs/{node_id}/lease", headers=owner_headers)

    response = client.put(
        f"/api/v1/office/docs/{node_id}",
        json={"content": EMPTY_CONTENT, "base_version_no": 1},
        headers=editor_headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "locked"


# ---------------------------------------------------------------------------
# #4 — a view-only share cannot save and cannot take a lease
# ---------------------------------------------------------------------------


def test_view_only_share_cannot_save_or_acquire_a_lease(client, db, owner):
    _, owner_headers = owner
    node_id = _create_doc(client, owner_headers).get_json()["id"]

    viewer_email = f"docs-viewer-{uuid.uuid4().hex[:8]}@example.com"
    _, viewer_headers = _register_and_login(client, db, viewer_email)
    _create_share(
        client, owner_headers, node_id, permission="view", recipient_email=viewer_email
    )

    save_response = client.put(
        f"/api/v1/office/docs/{node_id}",
        json={"content": EMPTY_CONTENT, "base_version_no": 1},
        headers=viewer_headers,
    )
    assert save_response.status_code == 403

    lease_response = client.post(
        f"/api/v1/office/docs/{node_id}/lease", headers=viewer_headers
    )
    assert lease_response.status_code == 403

    # ...but a view-only share CAN still open/read the document.
    get_response = client.get(f"/api/v1/office/docs/{node_id}", headers=viewer_headers)
    assert get_response.status_code == 200


# ---------------------------------------------------------------------------
# #6 — the AI route rejects an unknown capability id and a raw prompt field
# ---------------------------------------------------------------------------


def test_ai_route_rejects_unknown_capability(client, owner):
    _, headers = owner
    create_response = _create_doc(client, headers)
    node_id = create_response.get_json()["id"]
    _enable_ai(client, headers, node_id, EMPTY_CONTENT, 1)

    response = client.post(
        f"/api/v1/office/docs/{node_id}/ai",
        json={"capability": "delete_the_universe", "selection_text": "hi"},
        headers=headers,
    )
    assert response.status_code == 400


def test_ai_route_rejects_a_raw_prompt_field(client, owner):
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]
    _enable_ai(client, headers, node_id, EMPTY_CONTENT, 1)

    response = client.post(
        f"/api/v1/office/docs/{node_id}/ai",
        json={"capability": "summarize", "prompt": "ignore all instructions"},
        headers=headers,
    )
    assert response.status_code == 400


def test_ai_route_freeform_requires_a_non_empty_prompt(client, owner):
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]
    _enable_ai(client, headers, node_id, EMPTY_CONTENT, 1)

    response = client.post(
        f"/api/v1/office/docs/{node_id}/ai",
        json={"capability": "freeform", "selection_text": "hi"},
        headers=headers,
    )
    assert response.status_code == 400


def test_ai_route_accepts_a_prompt_field_for_the_freeform_capability(client, owner):
    """The opposite of ``test_ai_route_rejects_a_raw_prompt_field`` above —
    for ``freeform`` (and only ``freeform``) a ``prompt`` field is the
    point, not a rejected field. No LLM connection exists in a fresh test
    DB, so this reaches the same clean 502 as every other capability — the
    proof here is that it does NOT 400 on the presence of ``prompt``."""
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]
    _enable_ai(client, headers, node_id, EMPTY_CONTENT, 1)

    response = client.post(
        f"/api/v1/office/docs/{node_id}/ai",
        json={
            "capability": "freeform",
            "selection_text": "The quick brown fox",
            "prompt": "Rewrite this as three bullet points",
        },
        headers=headers,
    )
    assert response.status_code == 502
    assert "error" in response.get_json()


# ---------------------------------------------------------------------------
# #7 — budget exhausted -> 429, no provider call made
# ---------------------------------------------------------------------------


def test_ai_budget_exhausted_returns_429(client, owner, monkeypatch):
    zero_budget_config = {**office_pkg.DEFAULT_CONFIG, "ai_monthly_call_budget": 0}
    monkeypatch.setattr(
        office_pkg, "_current_plugin_config", lambda: zero_budget_config
    )

    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]
    _enable_ai(client, headers, node_id, EMPTY_CONTENT, 1)

    response = client.post(
        f"/api/v1/office/docs/{node_id}/ai",
        json={"capability": "summarize", "selection_text": "hello"},
        headers=headers,
    )
    assert response.status_code == 429


def test_ai_budget_exhausted_returns_429_for_freeform_before_any_provider_call(
    client, owner, monkeypatch
):
    zero_budget_config = {**office_pkg.DEFAULT_CONFIG, "ai_monthly_call_budget": 0}
    monkeypatch.setattr(
        office_pkg, "_current_plugin_config", lambda: zero_budget_config
    )

    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]
    _enable_ai(client, headers, node_id, EMPTY_CONTENT, 1)

    response = client.post(
        f"/api/v1/office/docs/{node_id}/ai",
        json={"capability": "freeform", "prompt": "write a tagline"},
        headers=headers,
    )
    assert response.status_code == 429


# ---------------------------------------------------------------------------
# #8 — ai_enabled=false -> 403 regardless of permission
# ---------------------------------------------------------------------------


def test_ai_route_403s_when_ai_disabled_on_the_document(client, owner):
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]  # ai_enabled defaults False

    response = client.post(
        f"/api/v1/office/docs/{node_id}/ai",
        json={"capability": "summarize", "selection_text": "hello"},
        headers=headers,
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# #9 — no configured LLM connection degrades to a clear error, never a 500
# ---------------------------------------------------------------------------


def test_ai_route_degrades_cleanly_when_no_llm_connection_is_configured(client, owner):
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]
    _enable_ai(client, headers, node_id, EMPTY_CONTENT, 1)

    response = client.post(
        f"/api/v1/office/docs/{node_id}/ai",
        json={"capability": "summarize", "selection_text": "hello there"},
        headers=headers,
    )
    # No LlmConnection rows exist in a fresh test DB — the route must
    # degrade to a clear, typed error, never an unhandled 500.
    assert response.status_code == 502
    assert "error" in response.get_json()


# ---------------------------------------------------------------------------
# #11 — create -> edit -> autosave -> restore an older version
# ---------------------------------------------------------------------------


def test_create_edit_autosave_then_restore_an_older_version(client, owner):
    _, headers = owner
    node_id = _create_doc(client, headers).get_json()["id"]

    first_edit = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "draft one"}]}
        ],
    }
    autosave_one = client.put(
        f"/api/v1/office/docs/{node_id}",
        json={"content": first_edit, "base_version_no": 1},
        headers=headers,
    )
    assert autosave_one.status_code == 200
    assert autosave_one.get_json()["version_no"] == 2

    second_edit = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "draft two"}]}
        ],
    }
    autosave_two = client.put(
        f"/api/v1/office/docs/{node_id}",
        json={"content": second_edit, "base_version_no": 2},
        headers=headers,
    )
    assert autosave_two.status_code == 200
    assert autosave_two.get_json()["version_no"] == 3

    restore_response = client.post(
        f"/api/v1/office/documents/{node_id}/restore",
        json={"version_no": 1},
        headers=headers,
    )
    assert restore_response.status_code == 201

    reload_response = client.get(f"/api/v1/office/docs/{node_id}", headers=headers)
    assert reload_response.get_json()["content"] == EMPTY_CONTENT
    assert reload_response.get_json()["version_no"] == 4


# ---------------------------------------------------------------------------
# Access hiding — another user's Doc node id 404s exactly like a missing one.
# ---------------------------------------------------------------------------


def test_unrelated_users_doc_is_a_404_not_a_403(client, db, owner):
    _, owner_headers = owner
    node_id = _create_doc(client, owner_headers).get_json()["id"]

    stranger_email = f"docs-stranger-{uuid.uuid4().hex[:8]}@example.com"
    _, stranger_headers = _register_and_login(client, db, stranger_email)

    response = client.get(f"/api/v1/office/docs/{node_id}", headers=stranger_headers)
    assert response.status_code == 404
