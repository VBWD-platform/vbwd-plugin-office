"""Services for the VBWD Office bundle.

Phase 0 (S147-00) ships two: ``OfficeMetaService`` (the enabled-not-merely-
mounted proof behind ``GET /api/v1/office/meta``) and the access-level seeder
that grants ``office.use`` additively. S147-1 adds ``OfficeDocumentService``,
``QuotaService`` and ``MimeSniffer``. S147-2 (sharing) adds
``AccessResolver`` (the single ACL truth source), ``SharingService`` (the
share/ACL orchestrator), and the small token/password/grant helpers it
composes (``share_token``, ``share_password``, ``share_grant``, ``ip_hash``).
S147-3 adds ``OfficeDocEditorService`` (the Docs orchestrator), the
``doc_content`` module (the structured JSON content model), the
``EditLeaseService`` (epic D4's single-writer soft lock), and the AI helper
(``ai_capabilities`` prompt templates + ``OfficeAiService``, riding the core
LLM Connection Manager).
"""
