"""Exceptions raised by the office document/node service layer (S147-1).

Every one of these maps to a route response that never discloses whether a
resource exists to a caller who is not its owner (test #9): a missing node
and someone else's node raise the exact same
:class:`OfficeNodeNotFoundError`.
"""


class OfficeNodeNotFoundError(Exception):
    """No node with this id is owned by the requesting user."""


class OfficeVersionNotFoundError(Exception):
    """No such version exists for this document."""


class OfficeUploadTooLargeError(Exception):
    """The uploaded payload exceeds the plugin's configured upload cap (B1)."""

    def __init__(self, size_bytes: int, max_upload_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.max_upload_bytes = max_upload_bytes
        super().__init__(
            f"Upload of {size_bytes} bytes exceeds the {max_upload_bytes}-byte cap"
        )


class OfficeInvalidNodeError(Exception):
    """A structurally invalid tree operation (e.g. moving a folder into its
    own descendant, or into itself)."""


# ---------------------------------------------------------------------------
# S147-2 (sharing) — every one of these maps to a route response that never
# discloses more than it must (C2): an invalid, revoked, or expired token
# all raise the exact same OfficeShareNotFoundError -> 404, indistinguishable
# from a token that was never issued at all.
# ---------------------------------------------------------------------------


class OfficeShareNotFoundError(Exception):
    """No LIVE share matches this id/token — covers "never existed",
    "revoked", and "expired" alike, on purpose (C2). Also used for an
    owner-side share id that exists but is not the caller's (mirrors
    ``OfficeNodeNotFoundError``'s existence-hiding contract)."""


class OfficeShareForbiddenError(Exception):
    """The resolved access level does not permit the attempted operation
    (e.g. a view/comment share attempting to write content)."""


class OfficeSharePasswordRequiredError(Exception):
    """A password-protected share was accessed without a valid unlock grant."""


class OfficeShareUnlockFailedError(Exception):
    """The password presented to ``POST /public/<token>/unlock`` was wrong."""


class OfficeShareInvalidPermissionError(Exception):
    """``permission`` is not one of view/comment/edit."""


class OfficeShareRecipientNotFoundError(Exception):
    """No user account matches the given recipient email."""


# ---------------------------------------------------------------------------
# S147-3 (VBWD Docs — editor, lease, AI helper)
# ---------------------------------------------------------------------------


class OfficeDocContentInvalidError(Exception):
    """A document's content model is not a well-formed, allow-listed node
    tree (epic D7 — never HTML, never an unknown node/mark type)."""


class OfficeDocStaleVersionError(Exception):
    """A save's ``base_version_no`` no longer matches the document's current
    version — the client must reload before retrying (never-trust-the-client,
    the same rule the platform already applies to money-adjacent actions)."""

    def __init__(self, current_version_no: int) -> None:
        self.current_version_no = current_version_no
        super().__init__(
            f"base_version_no is stale; current version is {current_version_no}"
        )


class OfficeDocLockedError(Exception):
    """A save was attempted while a LIVE edit lease is held by someone else
    (epic D4 — a second editor is read-only until the lease lapses)."""

    def __init__(self, holder_user_id, expires_at) -> None:
        self.holder_user_id = holder_user_id
        self.expires_at = expires_at
        super().__init__(f"Document is locked for editing until {expires_at}")


class OfficeAiDisabledError(Exception):
    """The document's ``ai_enabled`` flag is off (default for every
    document, including every shared one — the owner opts in)."""


class OfficeAiInvalidCapabilityError(Exception):
    """The requested capability id is not on the server-side allow-list, or
    the request is otherwise malformed for that capability (e.g. an empty
    selection, or an unsupported ``target_language``)."""


class OfficeAiBudgetExceededError(Exception):
    """The caller's per-user monthly AI-call budget (entitlement seam) is
    exhausted. Raised BEFORE any provider call is made."""

    def __init__(self, user_id, used: int, budget: int) -> None:
        self.user_id = user_id
        self.used = used
        self.budget = budget
        super().__init__(
            f"Monthly AI call budget exhausted for user {user_id}: {used}/{budget}"
        )


class OfficeAiProviderError(Exception):
    """The resolved LLM connection failed, or none could be resolved at
    all (no configured slug, no active default). Never surfaces a partial
    write to the document — the AI route never writes document content."""


# ---------------------------------------------------------------------------
# S147-4 (VBWD Spreadsheets — sparse workbook storage, ceiling, import/export)
# ---------------------------------------------------------------------------


class OfficeSheetContentInvalidError(Exception):
    """A stored/submitted workbook model is not well-formed: not the
    ``{sheets: [{name, cells: {...}}]}`` shape, an unrecognised cached-value
    encoding, or a change payload missing ``address``/``formula``/``value``."""


class OfficeSheetCeilingExceededError(Exception):
    """A cell reference falls outside the plugin's configured row/column
    ceiling (S147-4 requirement #4) — the route maps this to a clean ``413``,
    never an unbounded in-memory workbook."""

    def __init__(self, requested: int, ceiling: int, dimension: str) -> None:
        self.requested = requested
        self.ceiling = ceiling
        self.dimension = dimension
        super().__init__(
            f"Sheet {dimension} {requested} exceeds the configured ceiling of {ceiling}"
        )


class OfficeSheetImportFormatError(Exception):
    """``?format=`` (or the inferred extension) on an import is not one of
    the supported import formats."""


class OfficeSheetExportFormatError(Exception):
    """``?format=`` on an export is not one of the supported export formats."""


class OfficeSheetUnavailableFormatError(Exception):
    """A format is on the supported list but its optional dependency (e.g.
    ``openpyxl`` for ``.xlsx``) is not installed. Maps to a clean ``501`` —
    never a silently lossy export/import (S147-4 requirement #7's "ship
    .csv only and report .xlsx as not done" escape hatch, enforced in code
    rather than merely in a README)."""
