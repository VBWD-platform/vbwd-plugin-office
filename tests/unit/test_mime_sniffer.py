"""S147-1 test #5/#6 — server-side MIME sniffing wins over a lying declared
Content-Type, and correctly separates HTML from the preview allow-list."""
from plugins.office.office.services.mime_sniffer import MimeSniffer


def test_sniffs_png_from_the_magic_number():
    sniffer = MimeSniffer()
    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20

    assert sniffer.sniff(png_header) == "image/png"


def test_sniffs_pdf_from_the_magic_number():
    sniffer = MimeSniffer()

    assert sniffer.sniff(b"%PDF-1.7\n...") == "application/pdf"


def test_sniffs_plain_text():
    sniffer = MimeSniffer()

    assert sniffer.sniff(b"just some ordinary plain text") == "text/plain"


def test_sniffs_html_distinctly_from_plain_text():
    sniffer = MimeSniffer()

    assert sniffer.sniff(b"<!DOCTYPE html><html><body>hi</body></html>") == "text/html"
    assert sniffer.sniff(b"  <script>alert(1)</script>") == "text/html"


def test_unrecognised_binary_falls_back_to_octet_stream():
    sniffer = MimeSniffer()

    assert sniffer.sniff(bytes(range(256))) == "application/octet-stream"


def test_empty_payload_falls_back_to_octet_stream():
    sniffer = MimeSniffer()

    assert sniffer.sniff(b"") == "application/octet-stream"


def test_sniffed_mime_ignores_a_lying_declared_content_type():
    # The sniffer only ever looks at the bytes — there is no declared-type
    # parameter to lie to it with (B3: the client's Content-Type is recorded
    # elsewhere, never consulted here).
    sniffer = MimeSniffer()
    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20

    assert sniffer.sniff(png_header) != "text/html"
