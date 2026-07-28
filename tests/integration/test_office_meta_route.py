"""S147-00 — ``GET /api/v1/office/meta`` proves the plugin is ENABLED, not
merely mounted, and the filespace claim is written on enable."""


def test_meta_route_returns_the_plugin_and_version(client):
    response = client.get("/api/v1/office/meta")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["plugin"] == "office"
    assert payload["version"] == "0.1.0"
    assert payload["document_count"] == 0


def test_meta_route_requires_no_authentication(client):
    # A capability probe carries no personal data — declared public in
    # OfficePlugin.declare_public_routes so the S90 route-exposure oracle
    # does not flag it.
    response = client.get("/api/v1/office/meta")

    assert response.status_code != 401
    assert response.status_code != 403


def test_on_enable_claims_the_plugin_filespace(app):
    from plugins.office import FILESPACE_CLAIM_MARKER, PLUGIN_NAME

    with app.app_context():
        filespace = app.container.filesystem_manager().for_plugin(PLUGIN_NAME)
        assert filespace.exists(FILESPACE_CLAIM_MARKER)
        claim = filespace.read_json(FILESPACE_CLAIM_MARKER)
        assert claim["plugin"] == PLUGIN_NAME
