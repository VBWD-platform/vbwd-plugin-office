"""S147-3 toolbar bundle unit coverage for ``render_html_body`` — the
disposable HTML fragment PDF export renders from (never stored, never
re-parsed — see the module's own docstring). Pure function, no DB.
"""
from plugins.office.office.services.doc_export_html import render_html_body


def _paragraph(text: str, marks=None) -> dict:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text, "marks": marks or []}],
    }


def test_renders_headings_at_the_right_level():
    model = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Title"}],
            }
        ],
    }
    assert str(render_html_body(model)) == "<h2>Title</h2>"


def test_renders_bullet_and_ordered_lists():
    model = {
        "type": "doc",
        "content": [
            {
                "type": "bulletList",
                "content": [{"type": "listItem", "content": [_paragraph("one")]}],
            },
            {
                "type": "orderedList",
                "content": [{"type": "listItem", "content": [_paragraph("two")]}],
            },
        ],
    }
    html = str(render_html_body(model))
    assert "<ul><li><p>one</p></li></ul>" == html.split("<ol")[0]
    assert "<ol><li><p>two</p></li></ol>" in html


def test_renders_a_table_with_header_and_body_cells():
    model = {
        "type": "doc",
        "content": [
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [_paragraph("Name")]},
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [_paragraph("Ada")]}
                        ],
                    },
                ],
            }
        ],
    }
    html = str(render_html_body(model))
    assert (
        html
        == "<table><tr><th><p>Name</p></th></tr><tr><td><p>Ada</p></td></tr></table>"
    )


def test_renders_an_image_as_a_data_uri_via_the_resolver():
    model = {
        "type": "doc",
        "content": [
            {"type": "image", "attrs": {"src": "office-asset:asset-1", "alt": "a cat"}}
        ],
    }
    html = str(
        render_html_body(
            model, lambda asset_id: f"data:image/png;base64,FAKE-{asset_id}"
        )
    )
    assert html == '<img src="data:image/png;base64,FAKE-asset-1" alt="a cat">'


def test_omits_an_image_the_resolver_could_not_resolve():
    model = {
        "type": "doc",
        "content": [{"type": "image", "attrs": {"src": "office-asset:missing"}}],
    }
    html = str(render_html_body(model, lambda asset_id: None))
    assert html == ""


def test_a_pasted_script_tag_survives_as_inert_escaped_text():
    """Mirrors the sprint's XSS regression test (#5), applied to export."""
    model = {"type": "doc", "content": [_paragraph("<script>alert(1)</script>")]}
    html = str(render_html_body(model))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_bold_italic_underline_strike_and_code_marks_wrap_the_correct_tag():
    model = {
        "type": "doc",
        "content": [
            _paragraph("b", [{"type": "bold"}]),
            _paragraph("i", [{"type": "italic"}]),
            _paragraph("u", [{"type": "underline"}]),
            _paragraph("s", [{"type": "strike"}]),
            _paragraph("c", [{"type": "code"}]),
        ],
    }
    html = str(render_html_body(model))
    assert "<strong>b</strong>" in html
    assert "<em>i</em>" in html
    assert "<u>u</u>" in html
    assert "<s>s</s>" in html
    assert "<code>c</code>" in html


def test_a_link_mark_renders_an_anchor_with_the_escaped_href():
    model = {
        "type": "doc",
        "content": [
            _paragraph(
                "click me", [{"type": "link", "attrs": {"href": "https://example.com"}}]
            )
        ],
    }
    html = str(render_html_body(model))
    assert html == '<p><a href="https://example.com">click me</a></p>'


def test_a_textstyle_mark_renders_a_span_with_only_the_present_css_properties():
    model = {
        "type": "doc",
        "content": [
            _paragraph(
                "styled",
                [
                    {
                        "type": "textStyle",
                        "attrs": {"fontFamily": "Georgia", "fontSize": "18px"},
                    }
                ],
            )
        ],
    }
    html = str(render_html_body(model))
    assert (
        html
        == '<p><span style="font-family: Georgia; font-size: 18px">styled</span></p>'
    )


def test_a_textstyle_mark_with_no_recognised_attrs_wraps_nothing():
    model = {
        "type": "doc",
        "content": [_paragraph("plain", [{"type": "textStyle", "attrs": {}}])],
    }
    html = str(render_html_body(model))
    assert html == "<p>plain</p>"
