"""Unit coverage for the hardened ``sanitize_display_name`` (VBWD Space
Finder-parity slice): a display name can never carry a path separator, a
drive-letter/UNC-looking prefix, or be nothing but dots/whitespace, on top of
the pre-existing NUL/control-character stripping and the 255-char cap.

``DocumentStore.build_storage_key`` is built purely from server-minted UUIDs
+ an integer version number (see its module docstring) — a display name has
no path to influence where bytes land on disk even before this hardening.
This sanitiser exists to keep the name itself printable, bounded, and safe to
place inside an HTTP header value, and (as of this slice) free of anything
that LOOKS like a path.
"""
import pytest

from plugins.office.office.services.name_sanitizer import (
    DEFAULT_NODE_NAME,
    DISPLAY_NAME_MAX_LENGTH,
    sanitize_display_name,
)


def test_forward_slashes_are_stripped_not_preserved():
    result = sanitize_display_name("../../etc/passwd")

    assert "/" not in result
    assert result != ""


def test_backslashes_are_stripped_not_preserved():
    result = sanitize_display_name("..\\..\\windows")

    assert "\\" not in result
    assert result != ""


def test_a_single_path_segment_survives_readably():
    assert sanitize_display_name("foo/bar") == "foo bar"


@pytest.mark.parametrize("dots_only_name", [".", "..", "...", "   ", " . . "])
def test_dots_or_whitespace_only_names_fall_back_to_default(dots_only_name):
    assert sanitize_display_name(dots_only_name) == DEFAULT_NODE_NAME


def test_a_name_of_only_separators_falls_back_to_default():
    assert sanitize_display_name("////") == DEFAULT_NODE_NAME
    assert sanitize_display_name("\\\\\\\\") == DEFAULT_NODE_NAME


def test_drive_letter_prefix_is_stripped():
    result = sanitize_display_name("C:\\Windows\\System32")

    assert not result.upper().startswith("C:")
    assert "\\" not in result


def test_unc_looking_prefix_is_stripped():
    result = sanitize_display_name("\\\\server\\share\\secret.txt")

    assert "\\" not in result
    assert result != ""


def test_nul_bytes_are_still_stripped():
    assert "\x00" not in sanitize_display_name("evil\x00name")


def test_length_is_still_capped_at_255():
    result = sanitize_display_name("a" * 300)

    assert len(result) <= DISPLAY_NAME_MAX_LENGTH


def test_empty_and_none_still_fall_back_to_default():
    assert sanitize_display_name("") == DEFAULT_NODE_NAME
    assert sanitize_display_name(None) == DEFAULT_NODE_NAME


def test_a_normal_name_is_left_alone():
    assert sanitize_display_name("Quarterly Report.pdf") == "Quarterly Report.pdf"
