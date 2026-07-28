"""``MimeSniffer`` — server-side MIME detection from bytes only (S147-1 B3).

The stored ``mime_type`` is always derived from the file's own bytes; the
client-supplied ``Content-Type`` is recorded elsewhere (audit only) and never
consulted here. This decides preview vs. attachment download, which decides
whether a browser is ever allowed to execute the response — so it never
trusts anything the uploader claims.

Kept intentionally small: a short, ordered table of magic-number / structural
signatures covers the shapes VBWD Space needs (images, PDF, ZIP-based
formats, HTML, plain text); anything else is ``application/octet-stream``.
"""
from __future__ import annotations

DEFAULT_MIME_TYPE = "application/octet-stream"
MIME_TEXT_PLAIN = "text/plain"
MIME_TEXT_HTML = "text/html"

# Ordered (first match wins) magic-number signatures.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
)

# HTML is detected structurally (a handful of case-insensitive opening
# prefixes) so it is never mistaken for inert plain text — B4 relies on this
# to keep an uploaded ``.html`` file off the preview allow-list.
_HTML_PREFIXES = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
    b"<script",
    b"<svg",
)

_HTML_SNIFF_WINDOW = 512
_TEXT_SNIFF_WINDOW = 8192
_WEBP_RIFF_PREFIX = b"RIFF"
_WEBP_MARKER = b"WEBP"
_WEBP_MARKER_OFFSET = 8
_WEBP_MIN_LENGTH = 12


class MimeSniffer:
    """Stateless — one instance is safely reusable across requests."""

    def sniff(self, data: bytes) -> str:
        if not data:
            return DEFAULT_MIME_TYPE
        for signature, mime_type in _SIGNATURES:
            if data.startswith(signature):
                return mime_type
        if self._looks_like_webp(data):
            return "image/webp"
        if self._looks_like_html(data):
            return MIME_TEXT_HTML
        if self._looks_like_text(data):
            return MIME_TEXT_PLAIN
        return DEFAULT_MIME_TYPE

    @staticmethod
    def _looks_like_webp(data: bytes) -> bool:
        return (
            len(data) >= _WEBP_MIN_LENGTH
            and data.startswith(_WEBP_RIFF_PREFIX)
            and data[_WEBP_MARKER_OFFSET : _WEBP_MARKER_OFFSET + 4] == _WEBP_MARKER
        )

    @staticmethod
    def _looks_like_html(data: bytes) -> bool:
        window = data[:_HTML_SNIFF_WINDOW].lstrip().lower()
        return any(window.startswith(prefix) for prefix in _HTML_PREFIXES)

    @staticmethod
    def _looks_like_text(data: bytes) -> bool:
        sample = data[:_TEXT_SNIFF_WINDOW]
        if b"\x00" in sample:
            return False
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True
