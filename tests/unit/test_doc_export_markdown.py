"""S147-3 toolbar bundle unit coverage for ``render_markdown`` — the
"cheap" export format: a straightforward recursive walk of the SAME
allow-listed node tree ``doc_content.py`` validates. Pure function, no DB.
"""
from plugins.office.office.services.doc_export_markdown import render_markdown


def _paragraph(text: str, marks=None) -> dict:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text, "marks": marks or []}],
    }


def test_renders_headings_with_the_right_number_of_hashes():
    model = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 3},
                "content": [{"type": "text", "text": "Title"}],
            }
        ],
    }
    assert render_markdown(model) == "### Title\n"


def test_renders_a_bullet_list_and_an_ordered_list():
    model = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [_paragraph("one")]},
                    {"type": "listItem", "content": [_paragraph("two")]},
                ],
            },
            {
                "type": "orderedList",
                "content": [{"type": "listItem", "content": [_paragraph("first")]}],
            },
        ],
    }
    markdown = render_markdown(model)
    assert "- one\n- two" in markdown
    assert "1. first" in markdown


def test_renders_a_table_with_a_header_separator_row():
    model = {
        "type": "doc",
        "content": [
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [_paragraph("A")]},
                            {"type": "tableHeader", "content": [_paragraph("B")]},
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [_paragraph("1")]},
                            {"type": "tableCell", "content": [_paragraph("2")]},
                        ],
                    },
                ],
            }
        ],
    }
    markdown = render_markdown(model)
    assert markdown == "| A | B |\n| --- | --- |\n| 1 | 2 |\n"


def test_renders_an_image_as_a_reference_never_embedded_bytes():
    model = {
        "type": "doc",
        "content": [
            {"type": "image", "attrs": {"src": "office-asset:asset-1", "alt": "a cat"}}
        ],
    }
    assert render_markdown(model) == "![a cat](office-asset:asset-1)\n"


def test_bold_italic_strike_and_code_marks_use_the_expected_wrappers():
    model = {
        "type": "doc",
        "content": [
            _paragraph("b", [{"type": "bold"}]),
            _paragraph("i", [{"type": "italic"}]),
            _paragraph("s", [{"type": "strike"}]),
            _paragraph("c", [{"type": "code"}]),
            _paragraph("u", [{"type": "underline"}]),
        ],
    }
    markdown = render_markdown(model)
    assert "**b**" in markdown
    assert "_i_" in markdown
    assert "~~s~~" in markdown
    assert "`c`" in markdown
    assert "<u>u</u>" in markdown


def test_a_link_mark_renders_the_markdown_link_syntax():
    model = {
        "type": "doc",
        "content": [
            _paragraph(
                "click me", [{"type": "link", "attrs": {"href": "https://example.com"}}]
            )
        ],
    }
    assert render_markdown(model) == "[click me](https://example.com)\n"


def test_a_textstyle_mark_is_ignored_markdown_has_no_font_styling_concept():
    model = {
        "type": "doc",
        "content": [
            _paragraph(
                "plain",
                [
                    {
                        "type": "textStyle",
                        "attrs": {"fontFamily": "Georgia", "fontSize": "18px"},
                    }
                ],
            )
        ],
    }
    assert render_markdown(model) == "plain\n"


def test_a_horizontal_rule_renders_as_a_thematic_break():
    model = {"type": "doc", "content": [{"type": "horizontalRule"}]}
    assert render_markdown(model) == "---\n"


def test_an_empty_document_renders_as_an_empty_string():
    assert render_markdown({"type": "doc", "content": [{"type": "paragraph"}]}) == "\n"
