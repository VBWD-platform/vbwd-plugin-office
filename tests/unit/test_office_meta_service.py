"""S147-00 — the meta service that proves the plugin is ENABLED, not merely
mounted. S147-1/-2 replaced the phase-0 hardcoded document count with a real
one, injected as a counter so the service stays free of the data layer."""
from plugins.office.office.services.office_meta_service import (
    OfficeMeta,
    OfficeMetaService,
)


def test_build_meta_returns_the_plugin_and_version():
    service = OfficeMetaService(plugin_name="office", plugin_version="0.1.0")

    meta = service.build_meta()

    assert isinstance(meta, OfficeMeta)
    assert meta.plugin == "office"
    assert meta.version == "0.1.0"


def test_document_count_comes_from_the_injected_counter():
    service = OfficeMetaService(
        plugin_name="office",
        plugin_version="0.1.0",
        document_counter=lambda: 7,
    )

    assert service.build_meta().document_count == 7


def test_document_count_is_zero_when_no_counter_is_injected():
    # The meta route must still answer when the count cannot be resolved —
    # proving "enabled" must never depend on the data layer being reachable.
    service = OfficeMetaService(plugin_name="office", plugin_version="0.1.0")

    assert service.build_meta().document_count == 0


def test_a_failing_counter_degrades_to_zero_rather_than_500ing():
    """The probe's job is to answer. A broken count must not take it down.

    If counting raised, ``/meta`` would 500 — which is precisely the signal
    that means "disabled plugin, blueprint mounted without its providers".
    A meta route that 500s for an unrelated reason destroys that signal.
    """

    def exploding_counter() -> int:
        raise RuntimeError("database is unreachable")

    service = OfficeMetaService(
        plugin_name="office",
        plugin_version="0.1.0",
        document_counter=exploding_counter,
    )

    assert service.build_meta().document_count == 0


def test_to_dict_matches_the_meta_route_contract():
    service = OfficeMetaService(
        plugin_name="office",
        plugin_version="0.1.0",
        document_counter=lambda: 3,
    )

    payload = service.build_meta().to_dict()

    assert payload == {"plugin": "office", "version": "0.1.0", "document_count": 3}
