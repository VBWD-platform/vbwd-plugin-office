"""Exceptions raised by the office storage layer (S147-1)."""


class OfficeContentIntegrityError(Exception):
    """Raised when a stored blob's sha256 fails to match on read-back (B5).

    A mismatch is an error, never a warning — the byte-identical round-trip
    is the product this slice exists to guarantee.
    """
