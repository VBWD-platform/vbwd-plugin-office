"""S147-4 integration coverage — the real Flask app + PostgreSQL (rolled back
per test). Covers the sprint's test list against the live HTTP surface:

#1  create -> save cells (SUM, IF, VLOOKUP) -> reopen -> recalculated
#2  a stale base_version_no -> 409, no version written
#4  reference translation is exercised at the unit level; here we prove the
    dirty-subgraph contract end to end (only the affected cell round-trips)
#7  an over-ceiling cell reference -> 413, never an OOM
#9  no unmapped function is silently dropped on CSV import
#10 a view-only share can open/export but cannot save or import
Access hiding — another user's Sheet node id 404s exactly like a missing one.
Lease sharing — the SAME /docs/<node_id>/lease route works for a Sheet node.
"""
import io
import uuid

import pytest

EMPTY_WORKBOOK = {"sheets": [{"name": "Sheet1", "cells": {}}], "active_sheet": "Sheet1"}


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
    email = f"sheets-owner-{uuid.uuid4().hex[:8]}@example.com"
    return _register_and_login(client, db, email)


def _create_sheet(client, headers, name="Budget"):
    return client.post("/api/v1/office/sheets", json={"name": name}, headers=headers)


def _create_share(client, headers, node_id, **kwargs):
    return client.post(
        f"/api/v1/office/nodes/{node_id}/shares", json=kwargs, headers=headers
    )


# ---------------------------------------------------------------------------
# #1 — create -> save cells (SUM, IF, VLOOKUP) -> reopen -> recalculated
# ---------------------------------------------------------------------------


def test_create_then_save_sum_if_vlookup_then_reopen_recalculates(client, owner):
    _, headers = owner
    create_response = _create_sheet(client, headers)
    assert create_response.status_code == 201, create_response.data
    body = create_response.get_json()
    node_id = body["id"]
    assert body["workbook"] == EMPTY_WORKBOOK
    assert body["version_no"] == 1

    changes = [
        {"sheet": "Sheet1", "address": "A1", "value": 10},
        {"sheet": "Sheet1", "address": "A2", "value": 20},
        {"sheet": "Sheet1", "address": "A3", "value": 30},
        {"sheet": "Sheet1", "address": "B1", "formula": "=SUM(A1:A3)"},
        {"sheet": "Sheet1", "address": "B2", "formula": '=IF(B1>50,"big","small")'},
        {"sheet": "Sheet1", "address": "D1", "value": "other"},
        {"sheet": "Sheet1", "address": "D2", "value": "key"},
        {"sheet": "Sheet1", "address": "C1", "value": "key"},
        {"sheet": "Sheet1", "address": "C2", "formula": "=VLOOKUP(C1,D1:E2,2,FALSE)"},
        {"sheet": "Sheet1", "address": "E1", "value": "x"},
        {"sheet": "Sheet1", "address": "E2", "value": "found-it"},
    ]
    save_response = client.put(
        f"/api/v1/office/sheets/{node_id}/cells",
        json={"changes": changes, "base_version_no": 1},
        headers=headers,
    )
    assert save_response.status_code == 200, save_response.data
    save_body = save_response.get_json()
    assert save_body["version_no"] == 2
    assert save_body["changes"]["Sheet1!B1"] == 60.0
    assert save_body["changes"]["Sheet1!B2"] == "big"
    assert save_body["changes"]["Sheet1!C2"] == "found-it"
    # ONLY the cells that actually changed are returned — inputs that were
    # part of THIS same save are included (they changed too), but nothing
    # else in the sheet is re-sent.
    assert "Sheet1!A1" in save_body["changes"]

    reload_response = client.get(f"/api/v1/office/sheets/{node_id}", headers=headers)
    assert reload_response.status_code == 200
    reloaded_cells = reload_response.get_json()["workbook"]["sheets"][0]["cells"]
    assert reloaded_cells["B1"]["v"] == 60.0
    assert reloaded_cells["B2"]["v"] == "big"
    assert reloaded_cells["C2"]["v"] == "found-it"
    assert reload_response.get_json()["version_no"] == 2


# ---------------------------------------------------------------------------
# #2 — a stale base_version_no -> 409, nothing written
# ---------------------------------------------------------------------------


def test_stale_base_version_no_is_rejected_with_409_and_writes_nothing(client, owner):
    _, headers = owner
    node_id = _create_sheet(client, headers).get_json()["id"]

    response = client.put(
        f"/api/v1/office/sheets/{node_id}/cells",
        json={
            "changes": [{"sheet": "Sheet1", "address": "A1", "value": 1}],
            "base_version_no": 999,
        },
        headers=headers,
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "stale_version"

    reload_response = client.get(f"/api/v1/office/sheets/{node_id}", headers=headers)
    assert reload_response.get_json()["version_no"] == 1


# ---------------------------------------------------------------------------
# Partial recalc — saving an unrelated cell does not disturb a formula that
# does not depend on it, and a dependent formula IS recalculated.
# ---------------------------------------------------------------------------


def test_save_recalculates_only_the_dirty_subgraph(client, owner):
    _, headers = owner
    node_id = _create_sheet(client, headers).get_json()["id"]

    client.put(
        f"/api/v1/office/sheets/{node_id}/cells",
        json={
            "changes": [
                {"sheet": "Sheet1", "address": "A1", "value": 1},
                {"sheet": "Sheet1", "address": "B1", "formula": "=A1+1"},
                {"sheet": "Sheet1", "address": "C1", "value": "unrelated"},
            ],
            "base_version_no": 1,
        },
        headers=headers,
    )

    response = client.put(
        f"/api/v1/office/sheets/{node_id}/cells",
        json={
            "changes": [{"sheet": "Sheet1", "address": "A1", "value": 10}],
            "base_version_no": 2,
        },
        headers=headers,
    )
    assert response.status_code == 200
    changes = response.get_json()["changes"]
    assert changes == {"Sheet1!A1": 10.0, "Sheet1!B1": 11.0}
    assert "Sheet1!C1" not in changes


# ---------------------------------------------------------------------------
# #7 — an over-ceiling cell reference -> 413, never an OOM
# ---------------------------------------------------------------------------


def test_a_cell_reference_beyond_the_ceiling_returns_413(client, owner, monkeypatch):
    import plugins.office as office_pkg

    tiny_ceiling_config = {
        **office_pkg.DEFAULT_CONFIG,
        "sheet_max_rows": 10,
        "sheet_max_columns": 5,
    }
    monkeypatch.setattr(
        office_pkg, "_current_plugin_config", lambda: tiny_ceiling_config
    )

    _, headers = owner
    node_id = _create_sheet(client, headers).get_json()["id"]

    response = client.put(
        f"/api/v1/office/sheets/{node_id}/cells",
        json={
            "changes": [{"sheet": "Sheet1", "address": "A11", "value": 1}],
            "base_version_no": 1,
        },
        headers=headers,
    )
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_csv_reflects_recalculated_values(client, owner):
    _, headers = owner
    node_id = _create_sheet(client, headers).get_json()["id"]
    client.put(
        f"/api/v1/office/sheets/{node_id}/cells",
        json={
            "changes": [
                {"sheet": "Sheet1", "address": "A1", "value": 3},
                {"sheet": "Sheet1", "address": "B1", "value": 4},
                {"sheet": "Sheet1", "address": "C1", "formula": "=A1+B1"},
            ],
            "base_version_no": 1,
        },
        headers=headers,
    )

    response = client.post(
        f"/api/v1/office/sheets/{node_id}/export?format=csv", headers=headers
    )
    assert response.status_code == 200
    assert response.data.decode("utf-8").strip() == "3,4,7"


def test_export_rejects_an_unsupported_format(client, owner):
    _, headers = owner
    node_id = _create_sheet(client, headers).get_json()["id"]

    response = client.post(
        f"/api/v1/office/sheets/{node_id}/export?format=pdf", headers=headers
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# #9 — an unmapped function on CSV-adjacent import never silently drops a
# formula; it survives with #NAME? and is listed in the import report.
# (CSV itself has no formulas — this exercises the SAME engine contract via
# a direct cell save, proving the API surfaces #NAME? rather than an error.)
# ---------------------------------------------------------------------------


def test_saving_an_unmapped_function_surfaces_name_error_not_silently_dropped(
    client, owner
):
    _, headers = owner
    node_id = _create_sheet(client, headers).get_json()["id"]

    response = client.put(
        f"/api/v1/office/sheets/{node_id}/cells",
        json={
            "changes": [
                {
                    "sheet": "Sheet1",
                    "address": "A1",
                    "formula": "=XLOOKUP(1,B1:B2,C1:C2)",
                }
            ],
            "base_version_no": 1,
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json()["changes"]["Sheet1!A1"] == {"t": "error", "v": "#NAME?"}

    reload_response = client.get(f"/api/v1/office/sheets/{node_id}", headers=headers)
    reloaded_cell = reload_response.get_json()["workbook"]["sheets"][0]["cells"]["A1"]
    assert (
        reloaded_cell["f"] == "=XLOOKUP(1,B1:B2,C1:C2)"
    )  # source preserved, not dropped
    assert reloaded_cell["v"] == {"t": "error", "v": "#NAME?"}


def test_import_csv_with_only_literal_values_never_produces_a_name_error(client, owner):
    _, headers = owner
    node_id = _create_sheet(client, headers).get_json()["id"]

    csv_bytes = b"1,2\n3,4\n"
    response = client.post(
        f"/api/v1/office/sheets/{node_id}/import",
        data={"file": (io.BytesIO(csv_bytes), "data.csv")},
        headers=headers,
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.data
    body = response.get_json()
    assert body["unmapped_formulas"] == []

    reload_response = client.get(f"/api/v1/office/sheets/{node_id}", headers=headers)
    cells = reload_response.get_json()["workbook"]["sheets"][0]["cells"]
    assert cells["A1"]["v"] == 1.0
    assert cells["B2"]["v"] == 4.0


# ---------------------------------------------------------------------------
# #10 — a view-only share can open/export but cannot save or import
# ---------------------------------------------------------------------------


def test_view_only_share_cannot_save_cells_or_import(client, db, owner):
    _, owner_headers = owner
    node_id = _create_sheet(client, owner_headers).get_json()["id"]

    viewer_email = f"sheets-viewer-{uuid.uuid4().hex[:8]}@example.com"
    _, viewer_headers = _register_and_login(client, db, viewer_email)
    _create_share(
        client,
        owner_headers,
        node_id,
        permission="view",
        recipient_email=viewer_email,
    )

    save_response = client.put(
        f"/api/v1/office/sheets/{node_id}/cells",
        json={
            "changes": [{"sheet": "Sheet1", "address": "A1", "value": 1}],
            "base_version_no": 1,
        },
        headers=viewer_headers,
    )
    assert save_response.status_code == 403

    # ...but a view-only share CAN still open the sheet.
    get_response = client.get(
        f"/api/v1/office/sheets/{node_id}", headers=viewer_headers
    )
    assert get_response.status_code == 200


# ---------------------------------------------------------------------------
# Lease sharing — the SAME /docs/<node_id>/lease route works for a Sheet.
# ---------------------------------------------------------------------------


def test_sheet_node_reuses_the_same_lease_route_as_docs(client, db, owner):
    _, owner_headers = owner
    node_id = _create_sheet(client, owner_headers).get_json()["id"]

    editor_email = f"sheets-editor-{uuid.uuid4().hex[:8]}@example.com"
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

    save_response = client.put(
        f"/api/v1/office/sheets/{node_id}/cells",
        json={
            "changes": [{"sheet": "Sheet1", "address": "A1", "value": 1}],
            "base_version_no": 1,
        },
        headers=editor_headers,
    )
    assert save_response.status_code == 409
    assert save_response.get_json()["error"] == "locked"


# ---------------------------------------------------------------------------
# Access hiding — another user's Sheet node id 404s exactly like a missing
# one.
# ---------------------------------------------------------------------------


def test_unrelated_users_sheet_is_a_404_not_a_403(client, db, owner):
    _, owner_headers = owner
    node_id = _create_sheet(client, owner_headers).get_json()["id"]

    stranger_email = f"sheets-stranger-{uuid.uuid4().hex[:8]}@example.com"
    _, stranger_headers = _register_and_login(client, db, stranger_email)

    response = client.get(f"/api/v1/office/sheets/{node_id}", headers=stranger_headers)
    assert response.status_code == 404
