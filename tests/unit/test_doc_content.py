"""S147-3 unit coverage for the Doc content model — test #5 (a pasted
``<script>`` survives neither import nor render): the schema simply has no
node type that can carry markup, and the validator additionally rejects a
``javascript:`` link href and a non-asset image src (in-memory, no DB)."""
import pytest

from plugins.office.office.services.doc_content import (
    EMPTY_CONTENT_MODEL,
    empty_content_bytes,
    parse_content_bytes,
    serialize_content_model,
    validate_content_model,
)
from plugins.office.office.services.exceptions import OfficeDocContentInvalidError


def test_empty_content_model_is_valid():
    validate_content_model(EMPTY_CONTENT_MODEL)


def test_serialize_then_parse_round_trips_exactly():
    model = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "hello world"}],
            }
        ],
    }
    parsed = parse_content_bytes(serialize_content_model(model))
    assert parsed == model


def test_empty_content_bytes_round_trips():
    parsed = parse_content_bytes(empty_content_bytes())
    assert parsed == EMPTY_CONTENT_MODEL


def test_rejects_disallowed_node_type():
    model = {
        "type": "doc",
        "content": [{"type": "html", "text": "<script>x()</script>"}],
    }
    with pytest.raises(OfficeDocContentInvalidError):
        validate_content_model(model)


def test_a_literal_script_string_inside_a_text_node_is_harmless_content():
    """The model has no HTML node — a text node's ``text`` field is just a
    string, never interpreted as markup anywhere downstream, so it is
    perfectly valid to STORE (this is the structural guarantee behind D7,
    not a runtime blocklist of the substring)."""
    model = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "<script>alert(1)</script>"}],
            }
        ],
    }
    validate_content_model(model)  # does not raise


def test_rejects_javascript_scheme_link_href():
    model = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "click me",
                        "marks": [
                            {"type": "link", "attrs": {"href": "javascript:alert(1)"}}
                        ],
                    }
                ],
            }
        ],
    }
    with pytest.raises(OfficeDocContentInvalidError):
        validate_content_model(model)


def test_allows_https_link_href():
    model = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "click me",
                        "marks": [
                            {"type": "link", "attrs": {"href": "https://example.com"}}
                        ],
                    }
                ],
            }
        ],
    }
    validate_content_model(model)  # does not raise


def test_rejects_image_src_that_is_not_an_office_asset_reference():
    model = {
        "type": "doc",
        "content": [
            {"type": "image", "attrs": {"src": "https://evil.example/tracker.png"}}
        ],
    }
    with pytest.raises(OfficeDocContentInvalidError):
        validate_content_model(model)


def test_allows_office_asset_image_src():
    model = {
        "type": "doc",
        "content": [
            {
                "type": "image",
                "attrs": {"src": "office-asset:11111111-1111-1111-1111-111111111111"},
            }
        ],
    }
    validate_content_model(model)  # does not raise


def test_rejects_disallowed_mark_type():
    model = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "x", "marks": [{"type": "onclick"}]}
                ],
            }
        ],
    }
    with pytest.raises(OfficeDocContentInvalidError):
        validate_content_model(model)


def test_parse_content_bytes_rejects_invalid_json():
    with pytest.raises(OfficeDocContentInvalidError):
        parse_content_bytes(b"not json at all")


def test_validate_content_model_rejects_a_non_object():
    with pytest.raises(OfficeDocContentInvalidError):
        validate_content_model(["not", "an", "object"])
