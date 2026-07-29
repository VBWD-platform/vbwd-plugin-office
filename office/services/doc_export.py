"""``OfficeDocExportService`` — Print/PDF/DOCX/Markdown export for VBWD Docs
(S147-3 toolbar bundle).

Every format is rendered from the JSON content model, never from stored
HTML (epic D7 — the model is the one source of truth); the HTML this module
builds for PDF is a disposable, self-generated render target that exists
only for the length of one ``PdfService.render`` call (see
``doc_export_html.py``'s docstring). PDF rendering rides the CORE
``PdfService``/WeasyPrint seam every other plugin's PDF export already uses
(``plugins/booking/__init__.py`` is the precedent this class's template
registration mirrors) — no new PDF dependency, no second renderer.

Any resolvable access (owner/edit/comment/view) may export — printing or
downloading a document you can already read is not a write, so this does
NOT require ``EDIT_CAPABLE_ACCESS`` the way save/lease/asset-upload do.
"""
from __future__ import annotations

import base64
import os
from typing import Any, Callable, Dict, Optional, Tuple

from plugins.office.office.models.office_document import DOC_TYPE_TEXT
from plugins.office.office.services.doc_content import parse_content_bytes
from plugins.office.office.services.doc_export_docx import render_docx
from plugins.office.office.services.doc_export_html import render_html_body
from plugins.office.office.services.doc_export_markdown import render_markdown
from plugins.office.office.services.editable_node_resolver import (
    resolve_editable_document,
)
from plugins.office.office.services.exceptions import OfficeDocExportFormatError

EXPORT_FORMAT_PDF = "pdf"
EXPORT_FORMAT_DOCX = "docx"
EXPORT_FORMAT_MARKDOWN = "md"
SUPPORTED_EXPORT_FORMATS = (
    EXPORT_FORMAT_PDF,
    EXPORT_FORMAT_DOCX,
    EXPORT_FORMAT_MARKDOWN,
)

MARKDOWN_MIME_TYPE = "text/markdown"
PDF_MIME_TYPE = "application/pdf"
DOCX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

_PDF_TEMPLATE_NAME = "office_doc.html"
_PDF_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "templates", "pdf"
)


class OfficeDocExportService:
    def __init__(
        self,
        node_repository,
        document_repository,
        document_service,
        access_resolver,
        pdf_service,
    ) -> None:
        self._node_repository = node_repository
        self._document_repository = document_repository
        self._document_service = document_service
        self._access_resolver = access_resolver
        self._pdf_service = pdf_service

    def export_document(
        self, user_id, node_id, export_format: str
    ) -> Tuple[bytes, str, str]:
        if export_format not in SUPPORTED_EXPORT_FORMATS:
            raise OfficeDocExportFormatError(
                f"Unsupported export format: {export_format!r}"
            )

        node, document, _access = resolve_editable_document(
            self._access_resolver,
            self._node_repository,
            self._document_repository,
            user_id,
            node_id,
            allowed_doc_types=(DOC_TYPE_TEXT,),
        )
        _n, _d, _version, content_bytes = self._document_service.get_content_for_node(
            node, document
        )
        content_model = parse_content_bytes(content_bytes)

        if export_format == EXPORT_FORMAT_MARKDOWN:
            data = render_markdown(content_model).encode("utf-8")
            return data, MARKDOWN_MIME_TYPE, f"{node.name}.md"
        if export_format == EXPORT_FORMAT_DOCX:
            data = render_docx(
                content_model, node.name, self._asset_bytes_resolver(node)
            )
            return data, DOCX_MIME_TYPE, f"{node.name}.docx"
        data = self._render_pdf(node, content_model)
        return data, PDF_MIME_TYPE, f"{node.name}.pdf"

    # ------------------------------------------------------------------
    # PDF
    # ------------------------------------------------------------------

    def _render_pdf(self, node, content_model: Dict[str, Any]) -> bytes:
        resolver = self._asset_bytes_resolver(node)

        def data_uri_resolver(asset_node_id: str) -> Optional[str]:
            resolved = resolver(asset_node_id)
            if resolved is None:
                return None
            asset_bytes, mime_type = resolved
            encoded = base64.b64encode(asset_bytes).decode("ascii")
            return f"data:{mime_type};base64,{encoded}"

        body_html = render_html_body(content_model, data_uri_resolver)

        # Self-heal: mirrors plugins/booking/booking/routes.py — if
        # on_enable didn't run (e.g. a bare unit test), register the
        # template path before asking for the template rather than 500ing.
        self._pdf_service.register_plugin_template_path(_PDF_TEMPLATE_DIR)
        return self._pdf_service.render(
            _PDF_TEMPLATE_NAME, {"title": node.name, "body_html": body_html}
        )

    # ------------------------------------------------------------------
    # Shared asset resolution (PDF + DOCX embed the SAME way)
    # ------------------------------------------------------------------

    def _asset_bytes_resolver(self, doc_node) -> Callable[[str], Optional[tuple]]:
        def resolver(asset_node_id: str) -> Optional[tuple]:
            asset_node = self._node_repository.find_by_id_for_owner(
                asset_node_id, doc_node.owner_user_id
            )
            if asset_node is None:
                return None
            asset_document = self._document_repository.find_by_node_id(asset_node.id)
            if asset_document is None:
                return None
            _n, _d, _v, content = self._document_service.get_content_for_node(
                asset_node, asset_document
            )
            return content, asset_document.mime_type

        return resolver
