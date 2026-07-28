"""Doc content model — a structured JSON node tree, never HTML (S147-3,
epic D7).

A VBWD Docs document (``office_document.doc_type == 'text'``) stores a
ProseMirror/Tiptap-shaped JSON tree as its version bytes — the exact same
``office_version`` slot every other document kind uses (S147-1), just with a
JSON payload instead of an opaque file. There is no HTML node anywhere in the
allow-listed schema below, so rendering is a pure function of the model and
a pasted ``<script>`` cannot survive as anything other than inert TEXT (it is
never interpreted, never concatenated into a template, never sent to a
``v-html``-shaped sink anywhere in this plugin) — this is what keeps S147-2's
control C1 true for Docs rather than re-litigating it.

``validate_content_model`` is the one gate every write passes through
(``OfficeDocEditorService.save_document``) and the one gate every read is
re-checked against on the way out (``OfficeDocEditorService.open_document``,
so a row that somehow predates a schema tightening surfaces as a clear error
rather than a silent 200 with content the client cannot trust either).
"""
from __future__ import annotations

import json
from typing import Any, Dict

from plugins.office.office.services.exceptions import OfficeDocContentInvalidError

DOC_MODEL_TYPE = "doc"
TEXT_NODE_TYPE = "text"
IMAGE_NODE_TYPE = "image"

#: Node types a Doc may contain. No "html"/"rawHtml" type exists — this is
#: the structural guarantee behind D7, not a runtime blocklist of the string
#: "<script>" (plain text containing that string is harmless; it is never
#: rendered as markup anywhere in this plugin).
ALLOWED_NODE_TYPES = frozenset(
    {
        DOC_MODEL_TYPE,
        "paragraph",
        "heading",
        "bulletList",
        "orderedList",
        "listItem",
        "table",
        "tableRow",
        "tableCell",
        "tableHeader",
        IMAGE_NODE_TYPE,
        "codeBlock",
        "blockquote",
        "horizontalRule",
        "hardBreak",
        TEXT_NODE_TYPE,
    }
)

ALLOWED_MARK_TYPES = frozenset(
    {"bold", "italic", "underline", "strike", "code", "link"}
)

#: A link mark's ``href`` must start with one of these — rejects
#: ``javascript:``/``data:`` etc. (D7's XSS control applied to the one place
#: this model can carry a URL a click could follow).
ALLOWED_LINK_SCHEMES = ("http://", "https://", "mailto:")

#: An image node's ``src`` must reference an asset uploaded through the
#: existing vault upload path (a hidden ``_assets`` folder, epic doc) rather
#: than an arbitrary URL — one storage path, one quota, one ACL (DRY), and no
#: third-party tracking-pixel / SSRF surface via a shared Doc.
IMAGE_SRC_PREFIX = "office-asset:"

#: Defends against a pathologically deep/recursive payload; the byte-size cap
#: is already enforced upstream by the plugin's ordinary upload cap (the
#: content is written through the same ``add_version_for_node`` path as any
#: other file).
MAX_CONTENT_DEPTH = 32

EMPTY_CONTENT_MODEL: Dict[str, Any] = {
    "type": DOC_MODEL_TYPE,
    "content": [{"type": "paragraph"}],
}


def empty_content_bytes() -> bytes:
    """The initial content for a freshly created Doc (version 1)."""
    return serialize_content_model(EMPTY_CONTENT_MODEL)


def serialize_content_model(model: Dict[str, Any]) -> bytes:
    return json.dumps(model, separators=(",", ":")).encode("utf-8")


def parse_content_bytes(data: bytes) -> Dict[str, Any]:
    """Deserialise and validate a stored version's bytes back into a content
    model. Raises :class:`OfficeDocContentInvalidError` for anything that is
    not valid JSON or fails :func:`validate_content_model`."""
    try:
        model = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as parse_error:
        raise OfficeDocContentInvalidError(
            "Stored content is not valid JSON"
        ) from parse_error
    validate_content_model(model)
    return model


def validate_content_model(model: Any, *, _depth: int = 0) -> None:
    """Raise :class:`OfficeDocContentInvalidError` unless ``model`` is a
    well-formed node of the allow-listed schema, recursively."""
    if _depth > MAX_CONTENT_DEPTH:
        raise OfficeDocContentInvalidError(
            "Content tree exceeds the maximum nesting depth"
        )
    if not isinstance(model, dict):
        raise OfficeDocContentInvalidError("Every node must be a JSON object")

    node_type = model.get("type")
    if node_type not in ALLOWED_NODE_TYPES:
        raise OfficeDocContentInvalidError(f"Disallowed node type: {node_type!r}")

    if node_type == TEXT_NODE_TYPE:
        _validate_text_node(model)
        return

    if node_type == IMAGE_NODE_TYPE:
        _validate_image_attrs(model.get("attrs") or {})

    for child in model.get("content") or []:
        validate_content_model(child, _depth=_depth + 1)


def _validate_text_node(model: Dict[str, Any]) -> None:
    if not isinstance(model.get("text", ""), str):
        raise OfficeDocContentInvalidError("A text node's 'text' must be a string")
    _validate_marks(model.get("marks") or [])


def _validate_marks(marks: Any) -> None:
    if not isinstance(marks, list):
        raise OfficeDocContentInvalidError("'marks' must be a list")
    for mark in marks:
        if not isinstance(mark, dict) or mark.get("type") not in ALLOWED_MARK_TYPES:
            raise OfficeDocContentInvalidError(f"Disallowed mark type: {mark!r}")
        if mark.get("type") == "link":
            _validate_link_href((mark.get("attrs") or {}).get("href", ""))


def _validate_link_href(href: Any) -> None:
    if not isinstance(href, str) or not href.lower().startswith(ALLOWED_LINK_SCHEMES):
        raise OfficeDocContentInvalidError(f"Disallowed link href: {href!r}")


def _validate_image_attrs(attrs: Any) -> None:
    if not isinstance(attrs, dict):
        raise OfficeDocContentInvalidError("Image 'attrs' must be an object")
    src = attrs.get("src", "")
    if not isinstance(src, str) or not src.startswith(IMAGE_SRC_PREFIX):
        raise OfficeDocContentInvalidError(
            "Image src must reference an office asset (office-asset:<node_id>)"
        )
