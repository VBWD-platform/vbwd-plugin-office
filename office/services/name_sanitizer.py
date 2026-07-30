"""Display-name sanitisation shared by folder/document naming (S147-1, DRY;
hardened for VBWD Space Finder parity).

Nothing here touches storage paths — ``DocumentStore.build_storage_key`` is
built purely from server-minted UUIDs and an integer version number, so a
crafted display name has no path to influence (verified in
``tests/unit/test_document_service.py::test_rename_with_path_traversal_name_
does_not_change_storage_key``). This module exists to keep a *display* name
printable, bounded, and safe to place inside an HTTP header value
(``Content-Disposition``) — and, as of this hardening, free of anything that
merely LOOKS like a filesystem path, in case a future caller ever forgets
that rule.
"""
import re

DISPLAY_NAME_MAX_LENGTH = 255
DEFAULT_NODE_NAME = "Untitled"

# Runs of one or more path separators collapse to a single space (rather than
# vanishing outright) so "foo/bar" reads as "foo bar" instead of silently
# merging into "foobar".
_PATH_SEPARATOR_RUN_PATTERN = re.compile(r"[/\\]+")

# A leading Windows drive letter ("C:", "d:") — the colon is not a path
# separator itself, so the regex above would otherwise leave it dangling.
_DRIVE_LETTER_PREFIX_PATTERN = re.compile(r"^[A-Za-z]:")

# A name left with nothing but dots/spaces after the above (".", "..", "...",
# whitespace, or any combination) is indistinguishable from "no real name" —
# reject it the same way an empty name is rejected.
_DOTS_AND_SPACES_ONLY_PATTERN = re.compile(r"^[.\s]*$")


def sanitize_display_name(raw_name) -> str:
    """Strip control characters, NULs, path separators, and drive/UNC-looking
    prefixes; collapse to a single line; cap length. Falls back to
    :data:`DEFAULT_NODE_NAME` when nothing meaningful is left."""
    candidate = (raw_name or "").replace("\x00", "")
    candidate = "".join(
        character
        for character in candidate
        if character.isprintable() or character == " "
    )
    candidate = _DRIVE_LETTER_PREFIX_PATTERN.sub("", candidate)
    candidate = _PATH_SEPARATOR_RUN_PATTERN.sub(" ", candidate)
    candidate = candidate.strip()[:DISPLAY_NAME_MAX_LENGTH]
    if _DOTS_AND_SPACES_ONLY_PATTERN.match(candidate):
        return DEFAULT_NODE_NAME
    return candidate or DEFAULT_NODE_NAME


def sanitize_download_filename(raw_name) -> str:
    """A display name reduced further for safe use as a
    ``Content-Disposition`` filename: no path separators, no quotes."""
    candidate = sanitize_display_name(raw_name)
    for unsafe_character in ("/", "\\", '"'):
        candidate = candidate.replace(unsafe_character, "_")
    return candidate
