"""``OfficeMetaService`` — builds the payload behind ``GET /api/v1/office/meta``.

The meta route exists to prove the plugin is ENABLED, not merely mounted (a
disabled plugin whose blueprint still registers 500s instead of 404ing — the
distinction S146 paid for). Resolved through the DI container so the route
depends on the abstraction, not a hand-built instance (D — dependency
injection).

The document count arrives as an injected zero-argument ``document_counter``
rather than a repository, so this service never imports the data layer and stays
unit-testable without a session (DIP). S147-00 shipped a hardcoded ``0`` here;
S147-1 gave the plugin a real ``office_document`` table, so the placeholder is
gone.
"""
from dataclasses import dataclass
from typing import Callable, Optional

#: Reported when no counter is injected, or when counting fails. Answering with
#: a zero beats not answering: see ``build_meta``.
UNKNOWN_DOCUMENT_COUNT = 0


@dataclass(frozen=True)
class OfficeMeta:
    """The ``/meta`` response contract."""

    plugin: str
    version: str
    document_count: int

    def to_dict(self) -> dict:
        return {
            "plugin": self.plugin,
            "version": self.version,
            "document_count": self.document_count,
        }


class OfficeMetaService:
    """Builds :class:`OfficeMeta`. One reason to change: the meta contract."""

    def __init__(
        self,
        plugin_name: str,
        plugin_version: str,
        document_counter: Optional[Callable[[], int]] = None,
    ) -> None:
        self._plugin_name = plugin_name
        self._plugin_version = plugin_version
        self._document_counter = document_counter

    def build_meta(self) -> OfficeMeta:
        return OfficeMeta(
            plugin=self._plugin_name,
            version=self._plugin_version,
            document_count=self._count_documents(),
        )

    def _count_documents(self) -> int:
        """Return the document count, degrading to zero rather than raising.

        The probe's whole purpose is to distinguish "enabled" from "mounted but
        broken", and it signals that by answering at all. If an unrelated
        database problem made this route 500, it would emit exactly the symptom
        that means "disabled plugin" — destroying the signal it exists to give.
        So a counting failure costs the count, never the answer.
        """
        if self._document_counter is None:
            return UNKNOWN_DOCUMENT_COUNT
        try:
            return int(self._document_counter())
        except Exception:
            # Deliberately broad: see the docstring. Any failure to count must
            # cost the count, never the probe's answer.
            return UNKNOWN_DOCUMENT_COUNT
