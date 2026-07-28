"""VBWD Office — a self-hosted, privacy-first document suite (S147).

S147-1 (VBWD Space) adds the vault: folders, upload, download, versions and a
per-user storage quota — all owner-only. S147-2 adds sharing: an
``AccessResolver`` that is the single ACL truth source for every route
(owner-side and public alike), a ``SharingService`` orchestrating share CRUD
and the public read/write flows, and the ``/public/<token>*`` routes
declared here. S147-3 adds VBWD Docs: a Doc is an ``office_node`` of
``doc_type='text'`` reusing the same tree/versions/quota/ACL, plus an
``OfficeEditLeaseRepository``-backed edit lease (epic D4, single-writer, not
a CRDT) and an AI helper riding the core LLM Connection Manager. It claims
the plugin's filespace, mounts the vault + sharing + docs + capability-probe
routes, and grants its own RBAC vocabulary additively.

Class MUST be defined in this ``__init__.py`` (not re-exported): plugin
discovery requires ``obj.__module__ == "plugins.office"`` (manager.py).

``dependencies = []`` deliberately: the bundle stands alone. ``subscription``
is an optional runtime peer for quota tiers, resolved defensively through the
core entitlement seam (never a hard dependency), so this plugin's migration
root and CI clone-set never drag the subscription chain along.
"""
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from flask import current_app, has_app_context

from vbwd.plugins.base import BasePlugin, PluginMetadata, PublicRouteDeclaration
from vbwd.plugins.di_helpers import register_repositories, unregister_repositories

if TYPE_CHECKING:
    from flask import Blueprint

logger = logging.getLogger(__name__)

#: Single source of truth for the plugin id + version — routes.py and the DI
#: registration both read these rather than repeating the literals (DRY).
PLUGIN_NAME = "office"
PLUGIN_VERSION = "0.1.0"

#: The plugin's own filespace namespace (``var/office/``) and the marker file
#: written on enable so the seam is provably live from day one (acceptance #3
#: of S147-00: "var/office/ exists on disk and is written by the plugin
#: through its PluginFilespace").
FILESPACE_CLAIM_MARKER = "filespace_claim.json"

#: Container provider names for the four S147-1 repositories, plus the two
#: S147-2 sharing repositories and the two S147-3 editor/AI repositories
#: (``<plugin>_<repo_snake>`` per ``vbwd.plugins.di_helpers``, avoiding
#: collisions with core's own repository providers).
NODE_REPOSITORY_PROVIDER = "office_node_repository"
DOCUMENT_REPOSITORY_PROVIDER = "office_document_repository"
VERSION_REPOSITORY_PROVIDER = "office_version_repository"
USAGE_REPOSITORY_PROVIDER = "office_usage_repository"
SHARE_REPOSITORY_PROVIDER = "office_share_repository"
SHARE_ACCESS_REPOSITORY_PROVIDER = "office_share_access_repository"
EDIT_LEASE_REPOSITORY_PROVIDER = "office_edit_lease_repository"
AI_CALL_REPOSITORY_PROVIDER = "office_ai_call_repository"

DEFAULT_CONFIG: Dict[str, Any] = {
    "debug_mode": False,
    # Largest single file a user may upload into VBWD Space (S147-1 enforces
    # this in the upload path). Ops-tunable, never hardcoded at a call site.
    "max_upload_bytes": 52428800,
    # Total storage a free-tier user may hold across all documents (S147-1's
    # quota check, computed through the existing entitlement seam per D8).
    "free_quota_bytes": 1073741824,
    # MIME types the server renders an inline preview for; everything else
    # downloads as an attachment (D7 — never render attacker-supplied HTML).
    "preview_mime_allowlist": [
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "text/plain",
    ],
    # C2 — a single ops-tunable cap on every /public/<token>* route (metadata
    # read, content read/write, unlock). Deliberately one shared knob rather
    # than four (NO OVERENGINEERING) — the attack this guards against
    # (token/password enumeration) is the same shape for all four.
    "public_share_rate_limit_per_minute": 60,
    # C6 — how long an office_share_access audit row is kept. Enforced by
    # OfficeShareAccessRepository.delete_older_than(); not wired to a
    # scheduler in this slice (no cron infra to duplicate for one plugin).
    "share_access_log_retention_days": 90,
    # S147-3 (epic D4) — default edit-lease lifetime in seconds. A second
    # editor is read-only until this lapses (or the holder releases early).
    # Mirrors office.models.office_edit_lease.DEFAULT_LEASE_SECONDS (kept a
    # literal here, not imported, so this module — read at plugin discovery
    # time — never eagerly imports a SQLAlchemy model module).
    "edit_lease_seconds": 90,
    # S147-3 — the LLM connection SLUG the AI helper addresses (never an id).
    # Empty string resolves to the admin-configured active default
    # connection; an unknown/inactive slug also degrades to that default
    # rather than erroring (OfficeAiService._resolve_client).
    "ai_connection_slug": "",
    # S147-3 — per-user monthly AI-helper call budget (the entitlement seam's
    # "office.ai_monthly_call_budget" feature overrides this per plan).
    "ai_monthly_call_budget": 100,
    # S147-3 — hard caps (characters) on what is forwarded to the LLM,
    # independent of whatever length the client sent (data minimisation).
    "ai_max_selection_chars": 8000,
    "ai_max_context_chars": 2000,
    # S147-4 (VBWD Spreadsheets, requirement #4) — the row/column ceiling: a
    # cell reference beyond either is a clean 413, never an unbounded
    # in-memory workbook. 100,000 rows x 1,000 columns is generous for the
    # sparse-storage use case this slice targets (budgets, trackers, small
    # datasets) while keeping a worst-case dense recalculation pass — the
    # pure Python engine has no vectorised fast path — bounded on a single
    # request/worker. Ops-tunable, never hardcoded at a call site.
    "sheet_max_rows": 100000,
    "sheet_max_columns": 1000,
}


class OfficePlugin(BasePlugin):
    """VBWD Office — Space / Docs / Spreadsheets over one storage spine."""

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name=PLUGIN_NAME,
            version=PLUGIN_VERSION,
            author="VBWD Team",
            description=(
                "VBWD Office — a self-hosted, privacy-first substitute for the "
                "Google Docs / Microsoft 365 web suite (Space vault, Docs "
                "editor, Spreadsheets)."
            ),
            dependencies=[],
        )

    def initialize(self, config: Optional[Dict[str, Any]] = None) -> None:
        merged: Dict[str, Any] = {**DEFAULT_CONFIG}
        if config:
            merged.update(config)
        super().initialize(merged)

    def get_blueprint(self) -> Optional["Blueprint"]:
        from plugins.office.office.routes import office_bp

        return office_bp

    def get_url_prefix(self) -> Optional[str]:
        # Single prefix in this phase (only /meta), routes stay relative.
        return ""

    def declare_public_routes(self) -> PublicRouteDeclaration:
        """The capability probe carries no personal data and no document
        state — it exists so an installer/walkthrough can prove the plugin is
        ENABLED rather than merely mounted.

        The four ``/public/<token>*`` routes (S147-2, epic D6) are the
        product's entire point — a share link must work with no login at
        all — so each is declared here with its own justification rather
        than smuggled in. Every one of them still authorises itself
        in-handler via ``AccessResolver``/``SharingService`` before it
        reveals or accepts anything; "public" here means "reachable without
        @require_auth", not "unauthorised".
        """
        return PublicRouteDeclaration(
            read={
                "/api/v1/office/meta": (
                    "Capability probe for install verification — no personal "
                    "data, no document state."
                ),
                "/api/v1/office/public/<token>": (
                    "Anonymous share link: metadata for an allow_anonymous "
                    "share. Resolved through AccessResolver; an invalid, "
                    "revoked, expired, or anonymous-disallowed token is a "
                    "404 indistinguishable from a token that never existed "
                    "(C2)."
                ),
                "/api/v1/office/public/<token>/content": (
                    "Anonymous share link: streams content for a "
                    "view/comment/edit share. Always attachment-disposition "
                    "except the server-sniffed preview allow-list, never "
                    "text/html (C1 — the bundle's highest-severity risk)."
                ),
            },
            mutation={
                "/api/v1/office/public/<token>/unlock": (
                    "Password-protected share: exchanges a password for a "
                    "short-lived, share-scoped grant (C3) — never a session."
                ),
                "/api/v1/office/public/<token>/content": (
                    "Anonymous EDIT share: the owner explicitly enabled "
                    "logged-out editing when creating the share "
                    "(allow_anonymous=true, permission=edit). Charges the "
                    "OWNER's quota (C5) and attributes the write to the "
                    "share, never a user (C4)."
                ),
            },
        )

    @property
    def user_permissions(self) -> List[Dict[str, str]]:
        return [
            {
                "key": "office.use",
                "label": "Use VBWD Office",
                "group": "VBWD Office",
            },
        ]

    @property
    def admin_permissions(self) -> List[Dict[str, str]]:
        return [
            {
                "key": "office.documents.view",
                "label": "View VBWD Office documents",
                "group": "VBWD Office",
            },
            {
                "key": "office.documents.manage",
                "label": "Manage VBWD Office documents",
                "group": "VBWD Office",
            },
        ]

    def on_enable(self) -> None:
        """Claim the plugin's filespace and register its DI providers.

        A plugin whose blueprint mounts without its DI providers 500s instead
        of 404ing — that is the failure this method exists to prevent.

        Guarded on ``has_app_context()``: a bare unit test that enables the
        plugin directly (no request/app context) must not raise (Liskov: the
        degraded path never breaks the caller).
        """
        if not has_app_context():
            return
        container = getattr(current_app, "container", None)
        if container is None:
            return
        self._claim_filespace(container)
        self._register_di_providers(container)

    def _claim_filespace(self, container: Any) -> None:
        """Write the filespace claim marker so ``var/office/`` exists on disk
        from the moment the plugin is enabled — proven, not merely declared.
        """
        try:
            filespace = container.filesystem_manager().for_plugin(PLUGIN_NAME)
            filespace.write_json(
                FILESPACE_CLAIM_MARKER,
                {"plugin": PLUGIN_NAME, "version": PLUGIN_VERSION},
            )
        except Exception as filespace_error:  # noqa: BLE001 — never abort enable
            logger.warning("[office] Failed to claim filespace: %s", filespace_error)

    def _register_di_providers(self, container: Any) -> None:
        """Register ``office_meta_service`` and the four S147-1 repositories.

        A plugin whose blueprint mounts without its DI providers 500s instead
        of 404ing (the 2026-03-27 checkout outage) — this is the step that
        prevents it here.
        """
        from dependency_injector import providers

        from plugins.office.office.services.office_meta_service import (
            OfficeMetaService,
        )

        def count_documents() -> int:
            """Resolve the document repository lazily and count.

            Lazy on purpose: the repositories are registered further down this
            method, and each call needs a live request-scoped session, so the
            counter must resolve at call time rather than at wiring time.
            """
            repository_provider = getattr(container, DOCUMENT_REPOSITORY_PROVIDER)
            return repository_provider().count_all()

        container.office_meta_service = providers.Factory(
            OfficeMetaService,
            plugin_name=PLUGIN_NAME,
            plugin_version=PLUGIN_VERSION,
            document_counter=count_documents,
        )

        from plugins.office.office.repositories.office_document_repository import (
            OfficeDocumentRepository,
        )
        from plugins.office.office.repositories.office_node_repository import (
            OfficeNodeRepository,
        )
        from plugins.office.office.repositories.office_usage_repository import (
            OfficeUsageRepository,
        )
        from plugins.office.office.repositories.office_version_repository import (
            OfficeVersionRepository,
        )
        from plugins.office.office.repositories.office_share_repository import (
            OfficeShareRepository,
        )
        from plugins.office.office.repositories.office_share_access_repository import (
            OfficeShareAccessRepository,
        )
        from plugins.office.office.repositories.office_edit_lease_repository import (
            OfficeEditLeaseRepository,
        )
        from plugins.office.office.repositories.office_ai_call_repository import (
            OfficeAiCallRepository,
        )

        register_repositories(
            container,
            {
                NODE_REPOSITORY_PROVIDER: OfficeNodeRepository,
                DOCUMENT_REPOSITORY_PROVIDER: OfficeDocumentRepository,
                VERSION_REPOSITORY_PROVIDER: OfficeVersionRepository,
                USAGE_REPOSITORY_PROVIDER: OfficeUsageRepository,
                SHARE_REPOSITORY_PROVIDER: OfficeShareRepository,
                SHARE_ACCESS_REPOSITORY_PROVIDER: OfficeShareAccessRepository,
                EDIT_LEASE_REPOSITORY_PROVIDER: OfficeEditLeaseRepository,
                AI_CALL_REPOSITORY_PROVIDER: OfficeAiCallRepository,
            },
        )

    def on_disable(self) -> None:
        """Reverse the DI registration (no-op if absent)."""
        if not has_app_context():
            return
        container = getattr(current_app, "container", None)
        if container is None:
            return
        if hasattr(container, "office_meta_service"):
            delattr(container, "office_meta_service")
        unregister_repositories(
            container,
            [
                NODE_REPOSITORY_PROVIDER,
                DOCUMENT_REPOSITORY_PROVIDER,
                VERSION_REPOSITORY_PROVIDER,
                USAGE_REPOSITORY_PROVIDER,
                SHARE_REPOSITORY_PROVIDER,
                SHARE_ACCESS_REPOSITORY_PROVIDER,
                EDIT_LEASE_REPOSITORY_PROVIDER,
                AI_CALL_REPOSITORY_PROVIDER,
            ],
        )


# --------------------------------------------------------------------------
# Composition roots (S147-1) — resolved by routes.py per request, mirroring
# the ``build_dataset_*`` factories in ``plugins/dataset/__init__.py``. Kept
# as free functions (not container providers) because ``OfficeDocumentService``
# composes several collaborators that are themselves built here — one obvious
# place to read the wiring, matching the plugin's own established idiom.
# --------------------------------------------------------------------------


def _current_plugin_config() -> Dict[str, Any]:
    """Best-effort read of the live office plugin config (fallback: defaults).

    Ops-tunable values (upload cap, free quota, preview allow-list) live in
    the plugin config / ``var`` and are read here so no default is hardcoded
    at a call site.
    """
    try:
        config_store = getattr(current_app, "config_store", None)
        if config_store is not None:
            stored = config_store.get_config(PLUGIN_NAME)
            if stored:
                return {**DEFAULT_CONFIG, **stored}
    except Exception as config_read_error:  # noqa: BLE001 — never break a route
        logger.warning(
            "[office] Failed to read live plugin config: %s", config_read_error
        )
    return {**DEFAULT_CONFIG}


def build_document_store():
    """Composition root for :class:`DocumentStore` — the single home for
    read/write/delete + envelope encryption over the plugin's filespace."""
    from plugins.office.office.storage.document_store import DocumentStore

    filespace = current_app.container.filesystem_manager().for_plugin(PLUGIN_NAME)
    return DocumentStore(filespace)


def build_quota_service():
    """Composition root for :class:`QuotaService` (fresh ``db.session``)."""
    from vbwd.extensions import db

    from plugins.office.office.repositories.office_usage_repository import (
        OfficeUsageRepository,
    )
    from plugins.office.office.services.quota_service import QuotaService

    config = _current_plugin_config()
    return QuotaService(
        usage_repository=OfficeUsageRepository(db.session),
        free_quota_bytes=config.get(
            "free_quota_bytes", DEFAULT_CONFIG["free_quota_bytes"]
        ),
    )


def build_document_service():
    """Composition root for :class:`OfficeDocumentService` (fresh
    ``db.session``); the routes' single entry point into the vault."""
    from vbwd.extensions import db

    from plugins.office.office.repositories.office_document_repository import (
        OfficeDocumentRepository,
    )
    from plugins.office.office.repositories.office_node_repository import (
        OfficeNodeRepository,
    )
    from plugins.office.office.repositories.office_version_repository import (
        OfficeVersionRepository,
    )
    from plugins.office.office.services.document_service import OfficeDocumentService
    from plugins.office.office.services.mime_sniffer import MimeSniffer

    config = _current_plugin_config()
    return OfficeDocumentService(
        node_repository=OfficeNodeRepository(db.session),
        document_repository=OfficeDocumentRepository(db.session),
        version_repository=OfficeVersionRepository(db.session),
        quota_service=build_quota_service(),
        document_store=build_document_store(),
        mime_sniffer=MimeSniffer(),
        max_upload_bytes=config.get(
            "max_upload_bytes", DEFAULT_CONFIG["max_upload_bytes"]
        ),
    )


def preview_mime_allowlist() -> List[str]:
    """The live preview allow-list (B4) — everything else downloads as an
    attachment."""
    return _current_plugin_config().get(
        "preview_mime_allowlist", DEFAULT_CONFIG["preview_mime_allowlist"]
    )


def public_share_rate_limit() -> str:
    """The live per-minute cap for every ``/public/<token>*`` route (C2),
    formatted for ``flask_limiter``'s ``"<n> per minute"`` syntax."""
    limit = _current_plugin_config().get(
        "public_share_rate_limit_per_minute",
        DEFAULT_CONFIG["public_share_rate_limit_per_minute"],
    )
    return f"{limit} per minute"


def share_access_log_retention_days() -> int:
    """The live retention window (days) for ``office_share_access`` rows (C6)."""
    return _current_plugin_config().get(
        "share_access_log_retention_days",
        DEFAULT_CONFIG["share_access_log_retention_days"],
    )


def build_access_resolver():
    """Composition root for :class:`AccessResolver` (S147-2) — the single
    ACL truth source every owner-side and public route asks."""
    from vbwd.extensions import db

    from plugins.office.office.repositories.office_node_repository import (
        OfficeNodeRepository,
    )
    from plugins.office.office.repositories.office_share_repository import (
        OfficeShareRepository,
    )
    from plugins.office.office.services.access_resolver import AccessResolver

    return AccessResolver(
        node_repository=OfficeNodeRepository(db.session),
        share_repository=OfficeShareRepository(db.session),
    )


def build_sharing_service():
    """Composition root for :class:`SharingService` (fresh ``db.session``);
    the routes' single entry point into share CRUD and the public
    read/write flows."""
    from vbwd.extensions import db

    from vbwd.repositories.user_repository import UserRepository

    from plugins.office.office.repositories.office_document_repository import (
        OfficeDocumentRepository,
    )
    from plugins.office.office.repositories.office_node_repository import (
        OfficeNodeRepository,
    )
    from plugins.office.office.repositories.office_share_access_repository import (
        OfficeShareAccessRepository,
    )
    from plugins.office.office.repositories.office_share_repository import (
        OfficeShareRepository,
    )
    from plugins.office.office.services.share_grant import ShareGrantService
    from plugins.office.office.services.sharing_service import SharingService

    return SharingService(
        node_repository=OfficeNodeRepository(db.session),
        document_repository=OfficeDocumentRepository(db.session),
        share_repository=OfficeShareRepository(db.session),
        share_access_repository=OfficeShareAccessRepository(db.session),
        document_service=build_document_service(),
        access_resolver=build_access_resolver(),
        user_repository=UserRepository(db.session),
        grant_service=ShareGrantService(),
    )


# --------------------------------------------------------------------------
# Composition roots (S147-3) — the Docs editor + edit lease + AI helper.
# --------------------------------------------------------------------------


def build_edit_lease_service():
    """Composition root for :class:`EditLeaseService` (fresh ``db.session``)."""
    from vbwd.extensions import db

    from plugins.office.office.repositories.office_edit_lease_repository import (
        OfficeEditLeaseRepository,
    )
    from plugins.office.office.services.edit_lease_service import EditLeaseService

    config = _current_plugin_config()
    return EditLeaseService(
        lease_repository=OfficeEditLeaseRepository(db.session),
        lease_seconds=config.get(
            "edit_lease_seconds", DEFAULT_CONFIG["edit_lease_seconds"]
        ),
    )


def build_ai_service():
    """Composition root for :class:`OfficeAiService` — resolves the core
    ``llm_connection_service`` through the container (never builds a
    provider adapter itself)."""
    from vbwd.extensions import db

    from plugins.office.office.repositories.office_ai_call_repository import (
        OfficeAiCallRepository,
    )
    from plugins.office.office.services.ai_service import OfficeAiService

    config = _current_plugin_config()
    return OfficeAiService(
        llm_connection_service=current_app.container.llm_connection_service(),
        ai_call_repository=OfficeAiCallRepository(db.session),
        connection_slug=config.get(
            "ai_connection_slug", DEFAULT_CONFIG["ai_connection_slug"]
        ),
        default_monthly_call_budget=config.get(
            "ai_monthly_call_budget", DEFAULT_CONFIG["ai_monthly_call_budget"]
        ),
        max_selection_chars=config.get(
            "ai_max_selection_chars", DEFAULT_CONFIG["ai_max_selection_chars"]
        ),
        max_context_chars=config.get(
            "ai_max_context_chars", DEFAULT_CONFIG["ai_max_context_chars"]
        ),
    )


def build_admin_service():
    """Composition root for :class:`OfficeAdminService` (fresh
    ``db.session``) — the fe-admin catalogue + storage-overview read model."""
    from vbwd.extensions import db

    from plugins.office.office.repositories.office_document_repository import (
        OfficeDocumentRepository,
    )
    from plugins.office.office.repositories.office_usage_repository import (
        OfficeUsageRepository,
    )
    from plugins.office.office.services.office_admin_service import OfficeAdminService

    return OfficeAdminService(
        document_repository=OfficeDocumentRepository(db.session),
        usage_repository=OfficeUsageRepository(db.session),
    )


def build_doc_editor_service():
    """Composition root for :class:`OfficeDocEditorService` (fresh
    ``db.session``); the routes' single entry point into VBWD Docs."""
    from vbwd.extensions import db

    from plugins.office.office.repositories.office_document_repository import (
        OfficeDocumentRepository,
    )
    from plugins.office.office.repositories.office_node_repository import (
        OfficeNodeRepository,
    )
    from plugins.office.office.repositories.office_version_repository import (
        OfficeVersionRepository,
    )
    from plugins.office.office.services.doc_editor_service import OfficeDocEditorService

    return OfficeDocEditorService(
        node_repository=OfficeNodeRepository(db.session),
        document_repository=OfficeDocumentRepository(db.session),
        version_repository=OfficeVersionRepository(db.session),
        document_service=build_document_service(),
        access_resolver=build_access_resolver(),
        edit_lease_service=build_edit_lease_service(),
        ai_service=build_ai_service(),
    )


# --------------------------------------------------------------------------
# Composition root (S147-4) — VBWD Spreadsheets. Reuses the SAME node/
# document/version repositories, ``AccessResolver`` and ``EditLeaseService``
# composition roots above (DRY) — nothing Sheet-specific needed a new DI
# provider.
# --------------------------------------------------------------------------


def build_sheet_editor_service():
    """Composition root for :class:`OfficeSheetEditorService` (fresh
    ``db.session``); the routes' single entry point into VBWD Spreadsheets."""
    from vbwd.extensions import db

    from plugins.office.office.repositories.office_document_repository import (
        OfficeDocumentRepository,
    )
    from plugins.office.office.repositories.office_node_repository import (
        OfficeNodeRepository,
    )
    from plugins.office.office.repositories.office_version_repository import (
        OfficeVersionRepository,
    )
    from plugins.office.office.services.sheet_editor_service import (
        OfficeSheetEditorService,
    )

    config = _current_plugin_config()
    return OfficeSheetEditorService(
        node_repository=OfficeNodeRepository(db.session),
        document_repository=OfficeDocumentRepository(db.session),
        version_repository=OfficeVersionRepository(db.session),
        document_service=build_document_service(),
        access_resolver=build_access_resolver(),
        edit_lease_service=build_edit_lease_service(),
        max_rows=config.get("sheet_max_rows", DEFAULT_CONFIG["sheet_max_rows"]),
        max_columns=config.get(
            "sheet_max_columns", DEFAULT_CONFIG["sheet_max_columns"]
        ),
    )
