"""Content-model -> a TRUSTED, self-generated HTML fragment, used ONLY as a
disposable render target for PDF export (S147-3 toolbar bundle) — never
stored, never re-parsed, and built from nothing but the allow-listed node
tree ``doc_content.py`` already validated. This is not a re-litigation of
epic D7 ("never HTML, never stored"): the JSON content model stays the one
source of truth; this module's output exists only for the length of one
``PdfService.render`` call.

Every text run passes through :func:`markupsafe.escape` before it is placed
into the fragment, so a pasted ``<script>`` (or any other markup-looking
text a user typed) survives export as inert escaped entities — mirrors the
sprint's XSS regression test (#5), applied to the export path too, not just
storage/render.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from markupsafe import Markup, escape

from plugins.office.office.services.doc_content import IMAGE_SRC_PREFIX

#: The only CSS properties a ``textStyle`` mark's attrs may become —
#: ``doc_content.py``'s ``_validate_text_style_attrs`` already restricted
#: ``fontFamily``/``fontSize`` to a safe character set before this content
#: model could ever be stored, so building the ``style=""`` value here is a
#: defence-in-depth escape, not the only guard.
_TEXT_STYLE_CSS_PROPERTIES = {"fontFamily": "font-family", "fontSize": "font-size"}

#: Data-URI resolver: ``asset_node_id -> data:<mime>;base64,<...>`` (or
#: ``None`` when the asset cannot be resolved — the image is then simply
#: omitted, never a broken ``<img>`` tag pointing at an unreachable URL).
AssetDataUriResolver = Optional[Callable[[str], Optional[str]]]

_MARK_TAGS = {
    "bold": "strong",
    "italic": "em",
    "underline": "u",
    "strike": "s",
    "code": "code",
}


def render_html_body(
    content_model: Dict[str, Any], asset_data_uri_resolver: AssetDataUriResolver = None
) -> Markup:
    """Return a ``Markup``-wrapped HTML fragment for the document body —
    passed to the Jinja template as ``{{ body_html }}``; ``Markup`` tells
    Jinja's autoescape this string was already safely built and must not be
    escaped a second time (every piece of user text inside it already was,
    via :func:`markupsafe.escape`)."""
    return Markup(
        _render_blocks(content_model.get("content") or [], asset_data_uri_resolver)
    )


def _render_blocks(nodes: List[Dict[str, Any]], resolver: AssetDataUriResolver) -> str:
    return "".join(_render_block(node, resolver) for node in nodes)


def _render_block(node: Dict[str, Any], resolver: AssetDataUriResolver) -> str:
    node_type = node.get("type")
    children = node.get("content") or []
    if node_type == "paragraph":
        return f"<p>{_render_inline(children)}</p>"
    if node_type == "heading":
        level = min(max(int((node.get("attrs") or {}).get("level", 1)), 1), 6)
        return f"<h{level}>{_render_inline(children)}</h{level}>"
    if node_type == "bulletList":
        return f"<ul>{_render_list_items(children, resolver)}</ul>"
    if node_type == "orderedList":
        return f"<ol>{_render_list_items(children, resolver)}</ol>"
    if node_type == "blockquote":
        return f"<blockquote>{_render_blocks(children, resolver)}</blockquote>"
    if node_type == "codeBlock":
        return f"<pre><code>{escape(_extract_plain_text(node))}</code></pre>"
    if node_type == "table":
        return _render_table(node, resolver)
    if node_type == "image":
        return _render_image(node, resolver)
    if node_type == "horizontalRule":
        return "<hr>"
    return _render_blocks(children, resolver)  # the "doc" root, or an unknown wrapper


def _render_list_items(
    items: List[Dict[str, Any]], resolver: AssetDataUriResolver
) -> str:
    return "".join(
        f"<li>{_render_blocks(item.get('content') or [], resolver)}</li>"
        for item in items
    )


def _render_table(node: Dict[str, Any], resolver: AssetDataUriResolver) -> str:
    rows_html = []
    for row in node.get("content") or []:
        cells_html = []
        for cell in row.get("content") or []:
            tag = "th" if cell.get("type") == "tableHeader" else "td"
            cells_html.append(
                f"<{tag}>{_render_blocks(cell.get('content') or [], resolver)}</{tag}>"
            )
        rows_html.append(f"<tr>{''.join(cells_html)}</tr>")
    return f"<table>{''.join(rows_html)}</table>"


def _render_image(node: Dict[str, Any], resolver: AssetDataUriResolver) -> str:
    src = (node.get("attrs") or {}).get("src", "")
    if (
        not isinstance(src, str)
        or not src.startswith(IMAGE_SRC_PREFIX)
        or resolver is None
    ):
        return ""
    asset_node_id = src[len(IMAGE_SRC_PREFIX) :]
    data_uri = resolver(asset_node_id)
    if not data_uri:
        return ""
    alt = escape((node.get("attrs") or {}).get("alt", "") or "")
    return f'<img src="{escape(data_uri)}" alt="{alt}">'


def _render_inline(nodes: List[Dict[str, Any]]) -> str:
    return "".join(
        _render_text_node(node) for node in nodes if node.get("type") == "text"
    )


def _render_text_node(node: Dict[str, Any]) -> str:
    html = str(escape(node.get("text", "")))
    for mark in node.get("marks") or []:
        mark_type = mark.get("type")
        tag = _MARK_TAGS.get(mark_type)
        if tag:
            html = f"<{tag}>{html}</{tag}>"
        elif mark_type == "link":
            href = escape((mark.get("attrs") or {}).get("href", ""))
            html = f'<a href="{href}">{html}</a>'
        elif mark_type == "textStyle":
            html = _wrap_text_style(html, mark.get("attrs") or {})
    return html


def _wrap_text_style(html: str, attrs: Dict[str, Any]) -> str:
    declarations = [
        f"{css_property}: {escape(attrs[key])}"
        for key, css_property in _TEXT_STYLE_CSS_PROPERTIES.items()
        if attrs.get(key)
    ]
    if not declarations:
        return html
    return f'<span style="{"; ".join(declarations)}">{html}</span>'


def _extract_plain_text(node: Dict[str, Any]) -> str:
    parts: List[str] = []
    for child in node.get("content") or []:
        if child.get("type") == "text":
            parts.append(child.get("text", ""))
        elif child.get("type") == "hardBreak":
            parts.append("\n")
    return "".join(parts)
