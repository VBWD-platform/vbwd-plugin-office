"""Content-model -> Markdown rendering for VBWD Docs export (S147-3 toolbar
bundle). Markdown export is the sprint's explicitly "cheap" format — a
straightforward recursive walk of the SAME allow-listed node tree
``doc_content.py`` validates, never a second content representation.

An image node round-trips as ``![](office-asset:<node_id>)`` — a reference,
not embedded bytes; Markdown has no notion of an authorised, session-scoped
image fetch, so embedding here would either leak nothing useful (a bare
node id) or require inlining a data URI, which is what the PDF/DOCX
renderers are for. This is the documented "good-enough round-tripping, not
Word parity" trade-off from the epic, applied honestly to Markdown too.
"""
from __future__ import annotations

from typing import Any, Dict, List

_INLINE_MARK_WRAPPERS = {
    "bold": "**{}**",
    "italic": "_{}_",
    "strike": "~~{}~~",
    "code": "`{}`",
}


def render_markdown(content_model: Dict[str, Any]) -> str:
    blocks = _render_blocks(content_model.get("content") or [])
    return ("\n\n".join(block for block in blocks if block) + "\n") if blocks else ""


def _render_blocks(nodes: List[Dict[str, Any]]) -> List[str]:
    return [_render_block(node) for node in nodes]


def _render_block(node: Dict[str, Any]) -> str:
    node_type = node.get("type")
    children = node.get("content") or []
    if node_type == "paragraph":
        return _render_inline(children)
    if node_type == "heading":
        level = min(max(int((node.get("attrs") or {}).get("level", 1)), 1), 6)
        return f"{'#' * level} {_render_inline(children)}"
    if node_type == "bulletList":
        return "\n".join(f"- {_render_list_item_text(item)}" for item in children)
    if node_type == "orderedList":
        return "\n".join(
            f"{index}. {_render_list_item_text(item)}"
            for index, item in enumerate(children, start=1)
        )
    if node_type == "blockquote":
        quoted = "\n\n".join(_render_blocks(children))
        return "\n".join(f"> {line}" for line in quoted.splitlines())
    if node_type == "codeBlock":
        return f"```\n{_extract_plain_text(node)}\n```"
    if node_type == "table":
        return _render_table(node)
    if node_type == "image":
        return _render_image(node)
    if node_type == "horizontalRule":
        return "---"
    return "\n\n".join(_render_blocks(children))


def _render_list_item_text(item: Dict[str, Any]) -> str:
    return " ".join(_render_blocks(item.get("content") or []))


def _render_table(node: Dict[str, Any]) -> str:
    rows = node.get("content") or []
    if not rows:
        return ""
    rendered_rows = [_render_table_row(row) for row in rows]
    column_count = max((len(row) for row in rendered_rows), default=0)
    header = rendered_rows[0] + [""] * (column_count - len(rendered_rows[0]))
    separator = ["---"] * column_count
    lines = [_markdown_row(header), _markdown_row(separator)]
    for row in rendered_rows[1:]:
        padded = row + [""] * (column_count - len(row))
        lines.append(_markdown_row(padded))
    return "\n".join(lines)


def _render_table_row(row: Dict[str, Any]) -> List[str]:
    return [
        " ".join(_render_blocks(cell.get("content") or []))
        for cell in row.get("content") or []
    ]


def _markdown_row(cells: List[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def _render_image(node: Dict[str, Any]) -> str:
    src = (node.get("attrs") or {}).get("src", "")
    alt = (node.get("attrs") or {}).get("alt", "") or ""
    return f"![{alt}]({src})"


def _render_inline(nodes: List[Dict[str, Any]]) -> str:
    return "".join(
        _render_text_node(node) for node in nodes if node.get("type") == "text"
    )


def _render_text_node(node: Dict[str, Any]) -> str:
    text = node.get("text", "")
    marks = node.get("marks") or []
    for mark in marks:
        mark_type = mark.get("type")
        wrapper = _INLINE_MARK_WRAPPERS.get(mark_type)
        if wrapper:
            text = wrapper.format(text)
        elif mark_type == "underline":
            text = f"<u>{text}</u>"
        elif mark_type == "link":
            href = (mark.get("attrs") or {}).get("href", "")
            text = f"[{text}]({href})"
    return text


def _extract_plain_text(node: Dict[str, Any]) -> str:
    parts: List[str] = []
    for child in node.get("content") or []:
        if child.get("type") == "text":
            parts.append(child.get("text", ""))
        elif child.get("type") == "hardBreak":
            parts.append("\n")
    return "".join(parts)
