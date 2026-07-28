"""Repositories for the VBWD Office bundle.

S147-1 ships four, one per model: ``OfficeNodeRepository``,
``OfficeDocumentRepository``, ``OfficeVersionRepository``,
``OfficeUsageRepository``. S147-2 (sharing) adds ``OfficeShareRepository``
and ``OfficeShareAccessRepository``. S147-3 adds
``OfficeEditLeaseRepository`` and ``OfficeAiCallRepository``. Registered on
the DI container in ``OfficePlugin.on_enable`` (see
``plugins/office/__init__.py``).
"""
