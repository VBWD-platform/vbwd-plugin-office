"""VBWD Spreadsheets, exercised by a REAL financial model rather than toy cells.

A 24-month SaaS cash-flow projection starting at 100,000 MRR, built and computed
through the live HTTP surface. This is the acceptance demonstration for the
spreadsheet: a budget model is what people actually open a spreadsheet to build,
and it exercises the parts a toy `=1+1` never touches —

* a **cumulative chain**: each month's opening MRR is the previous month's
  closing (``=F3`` in ``B4``), so a single wrong dependency edge would cascade
  through 24 rows instead of hiding in one cell;
* **absolute references** to a driver block (``$O$2``), so the whole model
  re-forecasts from one assumption change;
* **range aggregation** (``=SUM(B4:E4)``) mixed with negative churn;
* a **derived cash balance** that accumulates EBITDA on top of opening cash.

The assertions are the arithmetic, not a snapshot: closing MRR must compound at
exactly the net growth rate, and cash must fall by the cumulative burn. A
regression in the dependency graph, the evaluation order, or `$`-anchoring
breaks these numbers rather than merely changing them.
"""
import pytest

# ── model drivers (kept here as ONE block, mirroring how the sheet stores them
# in column O — a model with rates inline in every formula cannot be re-run) ──
STARTING_MRR = 100_000.0
NEW_MRR_RATE = 0.08
EXPANSION_RATE = 0.02
CHURN_RATE = 0.03
COGS_RATE = 0.20
OPEX_RATE = 0.85  # S&M 45% + R&D 25% + G&A 15%
OPENING_CASH = 2_000_000.0
MONTHS = 24

#: 8% new + 2% expansion - 3% churn. Growth-stage SaaS with costs above revenue,
#: so the model shows a real burn and runway rather than a contrived break-even.
NET_GROWTH_RATE = NEW_MRR_RATE + EXPANSION_RATE - CHURN_RATE
EBITDA_MARGIN = 1.0 - COGS_RATE - OPEX_RATE  # negative: -5% of revenue


@pytest.fixture
def client(app):
    return app.test_client()


def _seed_office_use_permission(db):
    from vbwd.services.rbac_seeder import seed_default_rbac

    from plugins.office.office.services.access_seeder import grant_use_permission

    seed_default_rbac(db.session)
    grant_use_permission(db.session)
    db.session.commit()


def _register_and_login(client, db, email):
    """Register, ACTIVATE, then log in.

    The activation step is not optional: a freshly-registered user is not
    ACTIVE, and every subsequent request 401s with "Invalid or expired token"
    — which reads like an auth bug rather than the account state it actually
    is. Mirrors the helper in test_office_sheets_routes.py.
    """
    _seed_office_use_permission(db)
    password = "CashFlow123@"
    register_response = client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert register_response.status_code in (200, 201), register_response.data

    from vbwd.models.user import User

    user = db.session.query(User).filter_by(email=email).first()
    user.status = "ACTIVE"
    db.session.commit()

    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    body = response.get_json()
    token = body.get("token") or body.get("access_token")
    assert token, f"login did not return a token: {response.data}"
    return {"Authorization": f"Bearer {token}"}


def _driver_changes():
    """The assumption block, in column O — one home for every rate."""
    drivers = [
        ("O1", STARTING_MRR),
        ("O2", NEW_MRR_RATE),
        ("O3", EXPANSION_RATE),
        ("O4", CHURN_RATE),
        ("O5", COGS_RATE),
        ("O6", OPEX_RATE),
        ("O7", OPENING_CASH),
    ]
    return [{"address": address, "value": value} for address, value in drivers]


def _month_changes():
    """24 monthly rows of real formulas — no literals except the month number."""
    changes = []
    for month in range(1, MONTHS + 1):
        row = month + 1
        previous = row - 1
        changes.extend(
            [
                {"address": f"A{row}", "value": month},
                # Opening MRR: the driver in month 1, then last month's closing.
                {
                    "address": f"B{row}",
                    "formula": "=$O$1" if month == 1 else f"=F{previous}",
                },
                {"address": f"C{row}", "formula": f"=B{row}*$O$2"},  # new
                {"address": f"D{row}", "formula": f"=B{row}*$O$3"},  # expansion
                {"address": f"E{row}", "formula": f"=-B{row}*$O$4"},  # churn (negative)
                {"address": f"F{row}", "formula": f"=SUM(B{row}:E{row})"},  # closing
                {"address": f"G{row}", "formula": f"=F{row}*12"},  # ARR
                {"address": f"H{row}", "formula": f"=-F{row}*$O$5"},  # COGS
                {"address": f"I{row}", "formula": f"=-F{row}*$O$6"},  # opex
                {"address": f"J{row}", "formula": f"=F{row}+H{row}+I{row}"},  # EBITDA
                {
                    "address": f"K{row}",
                    "formula": f"=$O$7+J{row}"
                    if month == 1
                    else f"=K{previous}+J{row}",
                },
            ]
        )
    return changes


def _numeric(cells, address):
    """Unwrap a computed cell value, failing loudly on an error cell."""
    cell = cells.get(address) or {}
    value = cell.get("v")
    assert not isinstance(value, dict), f"{address} computed to an error: {value}"
    return value


def _build_model(client, headers):
    created = client.post(
        "/api/v1/office/sheets",
        json={"name": "SaaS cash-flow — 24 months"},
        headers=headers,
    )
    assert created.status_code == 201, created.data
    node_id = created.get_json()["id"]

    saved = client.put(
        f"/api/v1/office/sheets/{node_id}/cells",
        json={"base_version_no": 1, "changes": _driver_changes() + _month_changes()},
        headers=headers,
    )
    assert saved.status_code == 200, saved.data
    return node_id


def test_a_24_month_saas_cashflow_model_computes_end_to_end(client, db):
    headers = _register_and_login(client, db, "saas-model@example.com")
    node_id = _build_model(client, headers)

    # Reopen: the engine is the authority and recalculates every formula, so
    # these numbers are a fresh computation, not the cache written on save.
    reopened = client.get(f"/api/v1/office/sheets/{node_id}", headers=headers)
    assert reopened.status_code == 200
    cells = reopened.get_json()["workbook"]["sheets"][0]["cells"]

    # No cell anywhere in a 24-month model may be an error.
    errors = {
        address: cell["v"]
        for address, cell in cells.items()
        if isinstance(cell.get("v"), dict) and cell["v"].get("t") == "error"
    }
    assert not errors, f"model produced error cells: {errors}"

    # Month 1 opens at the driver, exactly.
    assert _numeric(cells, "B2") == pytest.approx(STARTING_MRR)

    # Closing MRR compounds at the net rate — the cumulative chain holds for 24
    # rows. A broken dependency edge shows up here as a wrong exponent.
    for month in (1, 6, 12, 24):
        row = month + 1
        expected = STARTING_MRR * (1 + NET_GROWTH_RATE) ** month
        assert _numeric(cells, f"F{row}") == pytest.approx(
            expected, rel=1e-9
        ), f"closing MRR wrong at month {month}"

    # MRR roughly quintuples over two years at 7%/month — a sanity check a
    # human would actually make about the business, not just the arithmetic.
    assert _numeric(cells, "F25") > 500_000
    # ARR is the closing MRR annualised.
    assert _numeric(cells, "G25") == pytest.approx(_numeric(cells, "F25") * 12)

    # EBITDA is negative every month at these cost rates, and the cash balance
    # falls monotonically from opening cash — the runway story.
    for month in (1, 12, 24):
        row = month + 1
        assert _numeric(cells, f"J{row}") == pytest.approx(
            _numeric(cells, f"F{row}") * EBITDA_MARGIN
        )
        assert _numeric(cells, f"J{row}") < 0

    assert _numeric(cells, "K2") < OPENING_CASH
    assert _numeric(cells, "K25") < _numeric(cells, "K2")
    # Still solvent after 24 months — the model is a burn, not a bankruptcy.
    assert _numeric(cells, "K25") > 0


def test_changing_one_driver_reforecasts_the_whole_model(client, db):
    """The point of `$O$2` rather than an inline 0.08: one edit re-forecasts.

    This is the assertion that would fail if absolute references were being
    translated like relative ones, or if the dirty sub-graph missed transitive
    dependents — the failure mode where a model looks fine until you change an
    assumption and only the first month moves.
    """
    headers = _register_and_login(client, db, "saas-driver@example.com")
    node_id = _build_model(client, headers)

    before = client.get(f"/api/v1/office/sheets/{node_id}", headers=headers).get_json()
    baseline_final_mrr = _numeric(before["workbook"]["sheets"][0]["cells"], "F25")

    # Halve new-MRR growth; every downstream month must re-forecast.
    bumped = client.put(
        f"/api/v1/office/sheets/{node_id}/cells",
        json={
            "base_version_no": before["version_no"],
            "changes": [{"address": "O2", "value": NEW_MRR_RATE / 2}],
        },
        headers=headers,
    )
    assert bumped.status_code == 200, bumped.data

    after = client.get(f"/api/v1/office/sheets/{node_id}", headers=headers).get_json()
    cells = after["workbook"]["sheets"][0]["cells"]

    slower_rate = (NEW_MRR_RATE / 2) + EXPANSION_RATE - CHURN_RATE
    expected_final = STARTING_MRR * (1 + slower_rate) ** MONTHS
    assert _numeric(cells, "F25") == pytest.approx(expected_final, rel=1e-9)
    assert _numeric(cells, "F25") < baseline_final_mrr


def test_the_model_exports_to_csv_and_xlsx_with_its_computed_values(client, db):
    headers = _register_and_login(client, db, "saas-export@example.com")
    node_id = _build_model(client, headers)

    csv_response = client.post(
        f"/api/v1/office/sheets/{node_id}/export?format=csv", headers=headers
    )
    assert csv_response.status_code == 200
    csv_text = csv_response.data.decode("utf-8")
    # A reader of the CSV must see the COMPUTED figures, not formula source.
    assert "=SUM(" not in csv_text
    assert "100000" in csv_text.replace(".0", "")

    xlsx_response = client.post(
        f"/api/v1/office/sheets/{node_id}/export?format=xlsx", headers=headers
    )
    assert xlsx_response.status_code == 200
    assert xlsx_response.data.startswith(b"PK")  # a real zip-container workbook
