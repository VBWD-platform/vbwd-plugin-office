"""S147-00 — plugin skeleton, registration, and public-route declaration."""
from vbwd.plugins.base import BasePlugin, PluginMetadata, PublicRouteDeclaration

from plugins.office import OfficePlugin


def test_plugin_is_a_base_plugin():
    assert issubclass(OfficePlugin, BasePlugin)


def test_metadata_declares_office_with_no_hard_dependencies():
    metadata = OfficePlugin().metadata
    assert isinstance(metadata, PluginMetadata)
    assert metadata.name == "office"
    assert metadata.version == "0.1.0"
    # No hard deps — a hard one would drag another plugin's migration chain
    # into CI, exactly what fragmented the graph in S146.
    assert metadata.dependencies == []


def test_user_permissions_declare_office_use():
    keys = {perm["key"] for perm in OfficePlugin().user_permissions}
    assert keys == {"office.use"}


def test_admin_permissions_declare_documents_view_and_manage():
    keys = {perm["key"] for perm in OfficePlugin().admin_permissions}
    assert keys == {"office.documents.view", "office.documents.manage"}


def test_blueprint_has_no_prefix_and_serves_the_meta_route():
    plugin = OfficePlugin()
    assert plugin.get_url_prefix() == ""
    blueprint = plugin.get_blueprint()
    assert blueprint is not None
    assert blueprint.name == "office"


def test_declares_the_meta_route_as_public():
    declaration = OfficePlugin().declare_public_routes()
    assert isinstance(declaration, PublicRouteDeclaration)
    assert "/api/v1/office/meta" in declaration.read


def test_declares_the_public_share_routes_with_justifications():
    """S147-2 D6 — every anonymous route, including the mutating ones, is
    declared with a written justification (never smuggled)."""
    declaration = OfficePlugin().declare_public_routes()

    assert "/api/v1/office/public/<token>" in declaration.read
    assert "/api/v1/office/public/<token>/content" in declaration.read
    assert "/api/v1/office/public/<token>/unlock" in declaration.mutation
    assert "/api/v1/office/public/<token>/content" in declaration.mutation

    all_entries = {**declaration.read, **declaration.mutation}
    assert all(bool(justification) for justification in all_entries.values())


def test_initialize_merges_default_config():
    plugin = OfficePlugin()
    plugin.initialize({})
    assert plugin.get_config("max_upload_bytes") == 52428800
    assert plugin.get_config("free_quota_bytes") == 1073741824
    assert "application/pdf" in plugin.get_config("preview_mime_allowlist")


def test_on_enable_without_a_container_does_not_raise():
    # Outside an app/request context ``current_app`` has no container — the
    # degraded path must not break the caller (Liskov).
    plugin = OfficePlugin()
    plugin.initialize({})
    plugin.on_enable()
    plugin.on_disable()
