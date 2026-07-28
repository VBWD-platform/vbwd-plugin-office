"""Display-name sanitisation shared by folder/document naming (S147-1, DRY).

Nothing here touches storage paths — ``DocumentStore.build_storage_key`` is
built purely from server-minted UUIDs and an integer version number, so a
crafted display name has no path to influence (test #2). This module exists
to keep a *display* name printable, bounded, and safe to place inside an HTTP
header value (``Content-Disposition``).
"""

DISPLAY_NAME_MAX_LENGTH = 255
DEFAULT_NODE_NAME = "Untitled"


def sanitize_display_name(raw_name) -> str:
    """Strip control characters and NULs, collapse to a single line, and cap
    length. Falls back to :data:`DEFAULT_NODE_NAME` when nothing printable is
    left."""
    candidate = (raw_name or "").replace("\x00", "")
    candidate = "".join(
        character
        for character in candidate
        if character.isprintable() or character == " "
    )
    candidate = candidate.strip()[:DISPLAY_NAME_MAX_LENGTH]
    return candidate or DEFAULT_NODE_NAME


def sanitize_download_filename(raw_name) -> str:
    """A display name reduced further for safe use as a
    ``Content-Disposition`` filename: no path separators, no quotes."""
    candidate = sanitize_display_name(raw_name)
    for unsafe_character in ("/", "\\", '"'):
        candidate = candidate.replace(unsafe_character, "_")
    return candidate
