"""VBWD Office HTTP surface.

``GET /api/v1/office/meta`` and the four ``/public/<token>*`` routes are the
plugin's declared public surface (``OfficePlugin.declare_public_routes``).
Every VBWD Space route otherwise is owner-only: ``@require_auth`` +
``@require_user_permission("office.use")``, with the owner resolved from
``g.user`` — never from the request body (the action payload never names the
actor). A node id that exists but belongs to someone else answers exactly the
same ``404`` as one that does not exist at all (test #9 — existence is never
disclosed); this falls out of ``OfficeNodeAccess``/the repositories filtering
on ``(id, owner_user_id)`` together in one query, not from anything in this
file re-checking ownership. The public share routes apply the identical
"never disclose more than 404" contract (C2) via ``SharingService`` +
``AccessResolver`` instead.

The public routes use ``@optional_auth`` (core middleware): a valid bearer
token loads ``g.user_id``/``g.user`` same as ``@require_auth``, but a
missing/invalid one is tolerated rather than 401ing — required for
``allow_anonymous=false`` link shares ("any logged-in account") and for a
fully anonymous visitor alike.
"""
import io

from flask import Blueprint, current_app, g, jsonify, request, send_file

from vbwd.extensions import db, limiter
from vbwd.middleware.auth import (
    optional_auth,
    require_admin,
    require_auth,
    require_permission,
    require_user_permission,
)
from vbwd.utils.pagination import paginate

from .services.access_seeder import USE_PERMISSION
from .services.document_service import PARENT_ID_UNSET
from .services.exceptions import (
    OfficeAiBudgetExceededError,
    OfficeAiDisabledError,
    OfficeAiInvalidCapabilityError,
    OfficeAiProviderError,
    OfficeDocAssetInvalidError,
    OfficeDocContentInvalidError,
    OfficeDocExportFormatError,
    OfficeDocExportUnavailableError,
    OfficeDocLockedError,
    OfficeDocStaleVersionError,
    OfficeDocTableImportFormatError,
    OfficeInvalidNodeError,
    OfficeNodeNotFoundError,
    OfficeSheetCeilingExceededError,
    OfficeSheetContentInvalidError,
    OfficeSheetExportFormatError,
    OfficeSheetImportFormatError,
    OfficeSheetUnavailableFormatError,
    OfficeShareForbiddenError,
    OfficeShareInvalidPermissionError,
    OfficeShareNotFoundError,
    OfficeSharePasswordRequiredError,
    OfficeShareRecipientNotFoundError,
    OfficeShareUnlockFailedError,
    OfficeUploadTooLargeError,
    OfficeVersionNotFoundError,
)
from .services.ip_hash import hash_client_ip
from .services.name_sanitizer import sanitize_download_filename
from .services.office_meta_service import OfficeMetaService
from .services.quota_service import OfficeQuotaExceededError
from .storage.exceptions import OfficeContentIntegrityError

office_bp = Blueprint("office", __name__)

PLAY = "/api/v1/office"


def _meta_service() -> OfficeMetaService:
    """Resolve the meta service through the container (DI), falling back to a
    directly-built instance outside an app/container context (e.g. a bare
    unit test)."""
    container = getattr(current_app, "container", None)
    if container is not None and hasattr(container, "office_meta_service"):
        return container.office_meta_service()
    from plugins.office import PLUGIN_NAME, PLUGIN_VERSION

    return OfficeMetaService(PLUGIN_NAME, PLUGIN_VERSION)


def _document_service():
    from plugins.office import build_document_service

    return build_document_service()


def _quota_service():
    from plugins.office import build_quota_service

    return build_quota_service()


def _sharing_service():
    from plugins.office import build_sharing_service

    return build_sharing_service()


def _doc_editor_service():
    from plugins.office import build_doc_editor_service

    return build_doc_editor_service()


def _sheet_editor_service():
    from plugins.office import build_sheet_editor_service

    return build_sheet_editor_service()


def _sheet_ai_service():
    from plugins.office import build_sheet_ai_service

    return build_sheet_ai_service()


def _doc_asset_service():
    from plugins.office import build_doc_asset_service

    return build_doc_asset_service()


def _doc_table_import_service():
    from plugins.office import build_doc_table_import_service

    return build_doc_table_import_service()


def _doc_export_service():
    from plugins.office import build_doc_export_service

    return build_doc_export_service()


def _admin_service():
    from plugins.office import build_admin_service

    return build_admin_service()


def _public_share_rate_limit() -> str:
    from plugins.office import public_share_rate_limit

    return public_share_rate_limit()


def _client_ip_hash():
    return hash_client_ip(request.remote_addr)


def _optional_user_id():
    return getattr(g, "user_id", None)


def _send_document_content(node, document, content: bytes):
    """Shared attachment/preview response builder (B4/C1), used by BOTH the
    owner-side and the public content routes — one source of truth (DRY).

    A mime NOT on the server-sniffed preview allow-list downloads as an
    attachment AND is re-advertised as ``application/octet-stream``. The second
    half matters: ``as_attachment`` plus ``nosniff`` already stops a modern
    browser rendering an uploaded ``.html``, but the stored mime is
    attacker-influenced and this is the one control whose failure mode is stored
    XSS on our own origin — served from a link the owner may have handed to
    strangers. So the response never carries an active content type it was not
    explicitly configured to preview."""
    from plugins.office import preview_mime_allowlist

    is_previewable = document.mime_type in preview_mime_allowlist()
    response = send_file(
        io.BytesIO(content),
        mimetype=(document.mime_type if is_previewable else "application/octet-stream"),
        as_attachment=not is_previewable,
        download_name=sanitize_download_filename(node.name),
        conditional=True,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _parse_expires_at(raw):
    """``None``/empty -> no expiry. Raises ``ValueError`` on a malformed
    timestamp — the route layer turns that into a 400."""
    if not raw:
        return None
    from datetime import datetime

    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)


@office_bp.route(f"{PLAY}/meta", methods=["GET"])
def office_meta():
    """Proves the plugin is ENABLED, not merely mounted."""
    return jsonify(_meta_service().build_meta().to_dict()), 200


@office_bp.route(f"{PLAY}/nodes", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def list_nodes():
    """List the children of ``?parent_id=`` (root when absent)."""
    parent_id = request.args.get("parent_id") or None
    try:
        items = _document_service().list_children(g.user.id, parent_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"items": [item.to_dict() for item in items]}), 200


@office_bp.route(f"{PLAY}/folders", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def create_folder():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        node = _document_service().create_folder(g.user.id, name, body.get("parent_id"))
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    db.session.commit()
    return jsonify(node.to_dict()), 201


@office_bp.route(f"{PLAY}/documents", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def upload_document():
    """Multipart upload -> creates a node + document + version 1."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "file is required"}), 400
    data = upload.stream.read()
    name = request.form.get("name") or upload.filename
    parent_id = request.form.get("parent_id") or None

    try:
        node, document, version = _document_service().upload_document(
            g.user.id, name, parent_id, data
        )
    except OfficeUploadTooLargeError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeQuotaExceededError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404

    db.session.commit()
    payload = node.to_dict()
    payload["document"] = document.to_dict()
    payload["version"] = version.to_dict()
    return jsonify(payload), 201


@office_bp.route(f"{PLAY}/documents/<node_id>", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def get_document(node_id):
    try:
        payload = _document_service().get_document_metadata(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    return jsonify(payload), 200


@office_bp.route(f"{PLAY}/documents/<node_id>/versions", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def add_document_version(node_id):
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "file is required"}), 400
    data = upload.stream.read()

    try:
        version = _document_service().add_version(g.user.id, node_id, data)
    except OfficeUploadTooLargeError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeQuotaExceededError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404

    db.session.commit()
    return jsonify(version.to_dict()), 201


@office_bp.route(f"{PLAY}/documents/<node_id>/versions", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def list_document_versions(node_id):
    try:
        versions = _document_service().list_versions(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"items": [version.to_dict() for version in versions]}), 200


@office_bp.route(f"{PLAY}/documents/<node_id>/content", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def download_document_content(node_id):
    """Stream a version's plaintext bytes (B4: attachment unless the
    server-sniffed mime is on the preview allow-list; B5: an integrity
    failure surfaces as an error, never silently served)."""
    version_param = request.args.get("version")
    try:
        version_no = int(version_param) if version_param is not None else None
    except ValueError:
        return jsonify({"error": "version must be an integer"}), 400

    try:
        node, document, _version, content = _document_service().get_content(
            g.user.id, node_id, version_no
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeVersionNotFoundError:
        return jsonify({"error": "Version not found"}), 404
    except OfficeContentIntegrityError:
        current_app.logger.error(
            "[office] content integrity check failed for node %s", node_id
        )
        return jsonify({"error": "Content integrity check failed"}), 500

    return _send_document_content(node, document, content)


@office_bp.route(f"{PLAY}/documents/<node_id>/restore", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def restore_document_version(node_id):
    """Restore a version = append a new one pointing at the old bytes."""
    body = request.get_json(silent=True) or {}
    version_no = body.get("version_no")
    if not isinstance(version_no, int):
        return jsonify({"error": "version_no is required"}), 400

    try:
        version = _document_service().restore_version(g.user.id, node_id, version_no)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeVersionNotFoundError:
        return jsonify({"error": "Version not found"}), 404

    db.session.commit()
    return jsonify(version.to_dict()), 201


@office_bp.route(f"{PLAY}/nodes/<node_id>", methods=["PATCH"])
@require_auth
@require_user_permission(USE_PERMISSION)
def update_node(node_id):
    """Rename and/or move (``parent_id: null`` moves to the vault root)."""
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    parent_id = body["parent_id"] if "parent_id" in body else PARENT_ID_UNSET

    try:
        node = _document_service().rename_or_move(g.user.id, node_id, name, parent_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeInvalidNodeError as error:
        return jsonify({"error": str(error)}), 400

    db.session.commit()
    return jsonify(node.to_dict()), 200


@office_bp.route(f"{PLAY}/nodes/<node_id>/copy", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def copy_node(node_id):
    """Duplicate a node into ``parent_id`` (the vault root when omitted).
    Finder vocabulary: a document copy is a new node/document/version-1
    through ``OfficeDocumentService`` (quota charged); a folder copy is
    recursive. A folder copy that would not fit its destination's quota
    writes nothing (413) — see ``copy_node``'s docstring."""
    body = request.get_json(silent=True) or {}

    try:
        node = _document_service().copy_node(g.user.id, node_id, body.get("parent_id"))
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeInvalidNodeError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeUploadTooLargeError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeQuotaExceededError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeContentIntegrityError:
        current_app.logger.error(
            "[office] copy content integrity check failed for node %s", node_id
        )
        return jsonify({"error": "Content integrity check failed"}), 500

    db.session.commit()
    return jsonify(node.to_dict()), 201


@office_bp.route(f"{PLAY}/nodes/<node_id>", methods=["DELETE"])
@require_auth
@require_user_permission(USE_PERMISSION)
def delete_node(node_id):
    """Trash (soft) by default; ``?purge=true`` permanently deletes and frees
    quota."""
    purge = request.args.get("purge", "false").lower() == "true"
    service = _document_service()
    try:
        if purge:
            service.purge_node(g.user.id, node_id)
        else:
            service.trash_node(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404

    db.session.commit()
    return "", 204


@office_bp.route(f"{PLAY}/usage", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def get_usage():
    quota_service = _quota_service()
    return (
        jsonify(
            {
                "bytes_used": quota_service.bytes_used(g.user.id),
                "bytes_quota": quota_service.quota_bytes(g.user.id),
            }
        ),
        200,
    )


# ---------------------------------------------------------------------------
# S147-2 — Owner-side share CRUD. Same auth bar as the vault routes above;
# ownership of the SHARE (not just the node) is enforced inside
# ``SharingService`` via ``OfficeShareRepository.find_by_id_for_owner``.
# ---------------------------------------------------------------------------


@office_bp.route(f"{PLAY}/nodes/<node_id>/shares", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def list_node_shares(node_id):
    try:
        shares = _sharing_service().list_shares(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"items": [share.to_dict() for share in shares]}), 200


@office_bp.route(f"{PLAY}/nodes/<node_id>/shares", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def create_node_share(node_id):
    body = request.get_json(silent=True) or {}
    try:
        expires_at = _parse_expires_at(body.get("expires_at"))
    except ValueError:
        return jsonify({"error": "expires_at must be an ISO-8601 timestamp"}), 400

    try:
        share, token = _sharing_service().create_share(
            g.user.id,
            node_id,
            permission=body.get("permission"),
            recipient_email=(body.get("recipient_email") or None),
            allow_anonymous=bool(body.get("allow_anonymous", False)),
            password=(body.get("password") or None),
            expires_at=expires_at,
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareInvalidPermissionError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeShareRecipientNotFoundError as error:
        return jsonify({"error": f"No account found for '{error}'"}), 404

    db.session.commit()
    payload = share.to_dict()
    payload["token"] = token  # shown exactly ONCE
    return jsonify(payload), 201


@office_bp.route(f"{PLAY}/shares/<share_id>", methods=["PATCH"])
@require_auth
@require_user_permission(USE_PERMISSION)
def update_share_route(share_id):
    body = request.get_json(silent=True) or {}
    update_kwargs = {}
    if "permission" in body:
        update_kwargs["permission"] = body["permission"]
    if "expires_at" in body:
        try:
            update_kwargs["expires_at"] = _parse_expires_at(body["expires_at"])
        except ValueError:
            return jsonify({"error": "expires_at must be an ISO-8601 timestamp"}), 400
    if "password" in body:
        update_kwargs["password"] = body["password"] or None
    if "allow_anonymous" in body:
        update_kwargs["allow_anonymous"] = bool(body["allow_anonymous"])

    try:
        share = _sharing_service().update_share(g.user.id, share_id, **update_kwargs)
    except OfficeShareNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareInvalidPermissionError as error:
        return jsonify({"error": str(error)}), 400

    db.session.commit()
    return jsonify(share.to_dict()), 200


@office_bp.route(f"{PLAY}/shares/<share_id>", methods=["DELETE"])
@require_auth
@require_user_permission(USE_PERMISSION)
def revoke_share_route(share_id):
    """Revoke — C7: this write is the ONLY thing that makes a share stop
    resolving; there is no cache layer anywhere in this slice to outlive it."""
    try:
        _sharing_service().revoke_share(g.user.id, share_id)
    except OfficeShareNotFoundError:
        return jsonify({"error": "Not found"}), 404

    db.session.commit()
    return "", 204


@office_bp.route(f"{PLAY}/shared-with-me", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def shared_with_me():
    pairs = _sharing_service().list_shared_with_me(g.user.id)
    items = []
    for share, node in pairs:
        entry = node.to_dict()
        entry["share"] = share.to_dict()
        items.append(entry)
    return jsonify({"items": items}), 200


@office_bp.route(f"{PLAY}/shares/<share_id>/access-log", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def share_access_log(share_id):
    try:
        entries = _sharing_service().access_log(g.user.id, share_id)
    except OfficeShareNotFoundError:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"items": [entry.to_dict() for entry in entries]}), 200


@office_bp.route(f"{PLAY}/shared-with-me/<node_id>", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def get_shared_document(node_id):
    """Open a document from "Shared with me" — session-only (no token): a
    named share is identified by the caller's own login, same as
    ``AccessResolver`` (D5). Owner-only nodes and nodes shared with someone
    else answer the same 404 as a node that does not exist (unchanged
    existence-hiding contract)."""
    try:
        payload = _sharing_service().get_shared_document_metadata(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    return jsonify(payload), 200


@office_bp.route(f"{PLAY}/shared-with-me/<node_id>/content", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def get_shared_document_content(node_id):
    try:
        (
            node,
            document,
            _version,
            content,
        ) = _sharing_service().get_shared_document_content(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeContentIntegrityError:
        current_app.logger.error(
            "[office] shared content integrity check failed for node %s", node_id
        )
        return jsonify({"error": "Content integrity check failed"}), 500

    return _send_document_content(node, document, content)


# ---------------------------------------------------------------------------
# S147-2 — Public share routes (declared in ``OfficePlugin.declare_public_
# routes``). ``@optional_auth`` loads g.user_id when a valid bearer token is
# presented but never 401s its absence — required for both the fully
# anonymous case and the "any logged-in account" case (D5).
# ---------------------------------------------------------------------------


@office_bp.route(f"{PLAY}/public/<token>", methods=["GET"])
@optional_auth
@limiter.limit(_public_share_rate_limit)
def get_public_share(token):
    try:
        result = _sharing_service().get_public_metadata(
            token, user_id=_optional_user_id(), ip_hash=_client_ip_hash()
        )
    except OfficeShareNotFoundError:
        return jsonify({"error": "Not found"}), 404

    node = result["node"]
    payload = {
        "name": node.name,
        "kind": node.kind,
        "permission": result["permission"],
        "requires_password": result["requires_password"],
    }
    if result["document"] is not None:
        payload["document"] = {
            "mime_type": result["document"].mime_type,
            "size_bytes": result["document"].size_bytes,
        }
    db.session.commit()
    return jsonify(payload), 200


@office_bp.route(f"{PLAY}/public/<token>/content", methods=["GET"])
@optional_auth
@limiter.limit(_public_share_rate_limit)
def get_public_share_content(token):
    grant_token = request.headers.get("X-Share-Grant")
    try:
        node, document, _version, content = _sharing_service().get_public_content(
            token,
            user_id=_optional_user_id(),
            grant_token=grant_token,
            ip_hash=_client_ip_hash(),
        )
    except OfficeShareNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeSharePasswordRequiredError:
        return jsonify({"error": "Password required"}), 401
    except OfficeContentIntegrityError:
        current_app.logger.error(
            "[office] public content integrity check failed for a share"
        )
        return jsonify({"error": "Content integrity check failed"}), 500

    db.session.commit()
    return _send_document_content(node, document, content)


@office_bp.route(f"{PLAY}/public/<token>/content", methods=["PUT"])
@optional_auth
@limiter.limit(_public_share_rate_limit)
def put_public_share_content(token):
    data = request.get_data()
    if not data:
        return jsonify({"error": "Request body is required"}), 400
    grant_token = request.headers.get("X-Share-Grant")

    try:
        version = _sharing_service().put_public_content(
            token,
            data,
            user_id=_optional_user_id(),
            grant_token=grant_token,
            ip_hash=_client_ip_hash(),
        )
    except OfficeShareNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareForbiddenError:
        return jsonify({"error": "Forbidden"}), 403
    except OfficeSharePasswordRequiredError:
        return jsonify({"error": "Password required"}), 401
    except OfficeUploadTooLargeError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeQuotaExceededError as error:
        return jsonify({"error": str(error)}), 413

    db.session.commit()
    return jsonify(version.to_dict()), 201


@office_bp.route(f"{PLAY}/public/<token>/unlock", methods=["POST"])
@optional_auth
@limiter.limit(_public_share_rate_limit)
def unlock_public_share(token):
    """C3: exchanges a password for a short-lived, share-scoped grant —
    never a session. The grant is returned in the body; the client re-sends
    it as ``X-Share-Grant`` on the content routes."""
    body = request.get_json(silent=True) or {}
    password = body.get("password") or ""

    try:
        grant_token = _sharing_service().unlock_share(
            token, password, ip_hash=_client_ip_hash()
        )
    except OfficeShareNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareUnlockFailedError:
        return jsonify({"error": "Invalid password"}), 401
    except OfficeSharePasswordRequiredError:
        return jsonify({"error": "This share does not require a password"}), 400

    db.session.commit()
    return jsonify({"grant_token": grant_token}), 200


# ---------------------------------------------------------------------------
# S147-3 — VBWD Docs (editor, edit lease, AI helper). A Doc is an
# office_node of doc_type='text'; every route here is reached only through a
# SESSION (owner or a named share) — never an anonymous share token — so
# every one is @require_auth like the vault routes above, never a public
# route (OfficeDocEditorService's module docstring explains why).
# ---------------------------------------------------------------------------


@office_bp.route(f"{PLAY}/docs", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def create_doc():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    try:
        view = _doc_editor_service().create_document(
            g.user.id, name, body.get("parent_id")
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeUploadTooLargeError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeQuotaExceededError as error:
        return jsonify({"error": str(error)}), 413

    db.session.commit()
    return jsonify(view.to_dict()), 201


@office_bp.route(f"{PLAY}/docs/<node_id>", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def get_doc(node_id):
    try:
        view = _doc_editor_service().open_document(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeDocContentInvalidError:
        current_app.logger.error(
            "[office] stored doc content invalid for node %s", node_id
        )
        return jsonify({"error": "Document content is corrupted"}), 500
    except OfficeContentIntegrityError:
        current_app.logger.error(
            "[office] doc content integrity check failed for node %s", node_id
        )
        return jsonify({"error": "Content integrity check failed"}), 500

    return jsonify(view.to_dict()), 200


@office_bp.route(f"{PLAY}/docs/<node_id>", methods=["PUT"])
@require_auth
@require_user_permission(USE_PERMISSION)
def save_doc(node_id):
    """Autosave / explicit save. ``base_version_no`` is the never-trust-the-
    client guard (test #2): a stale value is rejected with 409 and NOTHING is
    written — the same rule the platform already applies to money-adjacent
    actions."""
    body = request.get_json(silent=True) or {}
    content = body.get("content")
    base_version_no = body.get("base_version_no")
    if not isinstance(content, dict):
        return jsonify({"error": "content is required"}), 400
    if not isinstance(base_version_no, int):
        return jsonify({"error": "base_version_no is required"}), 400
    ai_enabled = body.get("ai_enabled") if "ai_enabled" in body else None

    try:
        version = _doc_editor_service().save_document(
            g.user.id, node_id, content, base_version_no, ai_enabled=ai_enabled
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareForbiddenError:
        return jsonify({"error": "Forbidden"}), 403
    except OfficeDocLockedError as locked_error:
        return (
            jsonify(
                {
                    "error": "locked",
                    "holder_user_id": str(locked_error.holder_user_id),
                    "expires_at": locked_error.expires_at.isoformat(),
                }
            ),
            409,
        )
    except OfficeDocStaleVersionError as stale_error:
        return (
            jsonify(
                {
                    "error": "stale_version",
                    "current_version_no": stale_error.current_version_no,
                }
            ),
            409,
        )
    except OfficeDocContentInvalidError as invalid_error:
        return jsonify({"error": str(invalid_error)}), 400
    except OfficeUploadTooLargeError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeQuotaExceededError as error:
        return jsonify({"error": str(error)}), 413

    db.session.commit()
    return jsonify(version.to_dict()), 200


@office_bp.route(f"{PLAY}/docs/<node_id>/lease", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def acquire_doc_lease(node_id):
    """Acquire, heartbeat-renew, or (once lapsed) take over the edit lease.
    ``granted=false`` in the body — not an error — means a live lease is held
    by someone else; the caller stays read-only (epic D4)."""
    try:
        lease_state = _doc_editor_service().acquire_lease(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareForbiddenError:
        return jsonify({"error": "Forbidden"}), 403

    db.session.commit()
    return jsonify(lease_state.to_dict()), 200


@office_bp.route(f"{PLAY}/docs/<node_id>/lease", methods=["DELETE"])
@require_auth
@require_user_permission(USE_PERMISSION)
def release_doc_lease(node_id):
    """No-op (204) unless the caller is the CURRENT holder."""
    try:
        _doc_editor_service().release_lease(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404

    db.session.commit()
    return "", 204


@office_bp.route(f"{PLAY}/docs/<node_id>/presence", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def doc_presence(node_id):
    """Poll target for the lease banner (deliberately polling, not SSE — a
    long-lived stream per open document starves gunicorn workers exactly as
    it did for meinchat; a 5s client poll is not a product failure here)."""
    try:
        lease_state = _doc_editor_service().presence(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404

    return jsonify(lease_state.to_dict()), 200


@office_bp.route(f"{PLAY}/docs/<node_id>/ai", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def doc_ai(node_id):
    """The AI helper. ``capability`` is the ONLY instruction the client may
    send (test #6) — a ``prompt`` field is rejected outright, before the
    service layer is even reached, so this plugin can never become an open
    proxy to the operator's paid LLM connection."""
    body = request.get_json(silent=True) or {}
    if "prompt" in body:
        return (
            jsonify({"error": "Raw prompts are not accepted; use 'capability'"}),
            400,
        )
    capability = body.get("capability")
    if not isinstance(capability, str) or not capability:
        return jsonify({"error": "capability is required"}), 400

    try:
        result = _doc_editor_service().run_ai_capability(
            g.user.id,
            node_id,
            capability,
            selection_text=body.get("selection_text", ""),
            context_before=body.get("context_before", ""),
            context_after=body.get("context_after", ""),
            target_language=body.get("target_language"),
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareForbiddenError:
        return jsonify({"error": "Forbidden"}), 403
    except OfficeAiDisabledError:
        return jsonify({"error": "AI helper is disabled for this document"}), 403
    except OfficeAiInvalidCapabilityError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeAiBudgetExceededError as error:
        return jsonify({"error": str(error)}), 429
    except OfficeAiProviderError as error:
        current_app.logger.error("[office] AI request failed: %s", error)
        return jsonify({"error": "AI request failed"}), 502

    db.session.commit()
    return jsonify(result), 200


# ---------------------------------------------------------------------------
# S147-3 toolbar bundle — image assets, "insert table from file", and
# export (PDF/DOCX/Markdown) for VBWD Docs. Same session-only auth bar as
# the rest of Docs above; access decisions delegate to the SAME
# AccessResolver via the new Doc-specific services (doc_assets.py,
# doc_table_import.py, doc_export.py).
# ---------------------------------------------------------------------------


@office_bp.route(f"{PLAY}/docs/<node_id>/assets", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def upload_doc_asset(node_id):
    """Insert-image: uploads into the caller's hidden ``_assets`` folder and
    returns the ``office-asset:<id>`` src the editor writes into an image
    node — never a public URL (one storage path, one quota, one ACL)."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "file is required"}), 400
    data = upload.stream.read()

    try:
        result = _doc_asset_service().upload_asset(
            g.user.id, node_id, upload.filename, data
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareForbiddenError:
        return jsonify({"error": "Forbidden"}), 403
    except OfficeDocAssetInvalidError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeUploadTooLargeError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeQuotaExceededError as error:
        return jsonify({"error": str(error)}), 413

    db.session.commit()
    return jsonify(result), 201


@office_bp.route(f"{PLAY}/docs/<node_id>/assets/<asset_node_id>", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def get_doc_asset(node_id, asset_node_id):
    """Streams an inserted image's bytes — reachable by anyone with at
    least ``view`` access to the DOC (not just the asset owner), which is
    what makes a shared Doc's images render for the recipient. Only an
    asset actually referenced by THIS document's current content is ever
    served (see doc_assets.py's module docstring)."""
    try:
        asset_node, asset_document, _version, content = _doc_asset_service().get_asset(
            g.user.id, node_id, asset_node_id
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeDocContentInvalidError:
        current_app.logger.error(
            "[office] stored doc content invalid for node %s", node_id
        )
        return jsonify({"error": "Document content is corrupted"}), 500
    except OfficeContentIntegrityError:
        current_app.logger.error(
            "[office] doc asset content integrity check failed for node %s", node_id
        )
        return jsonify({"error": "Content integrity check failed"}), 500

    return _send_document_content(asset_node, asset_document, content)


DOC_TABLE_IMPORT_FORMATS = ("csv", "xlsx")


@office_bp.route(f"{PLAY}/docs/<node_id>/table-import", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def import_doc_table(node_id):
    """Recognise a table from an uploaded .csv/.xlsx and return it as JSON
    rows for the editor to insert as a real table node — REUSES
    sheet_import_export.py's parser (DRY), never a second one."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "file is required"}), 400

    import_format = (request.form.get("format") or "").lower()
    if not import_format:
        import_format = "xlsx" if upload.filename.lower().endswith(".xlsx") else "csv"
    if import_format not in DOC_TABLE_IMPORT_FORMATS:
        return (
            jsonify(
                {"error": f"format must be one of {list(DOC_TABLE_IMPORT_FORMATS)}"}
            ),
            400,
        )
    data = upload.stream.read()

    try:
        table = _doc_table_import_service().import_table(
            g.user.id, node_id, data, import_format
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareForbiddenError:
        return jsonify({"error": "Forbidden"}), 403
    except OfficeDocTableImportFormatError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeSheetContentInvalidError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeSheetCeilingExceededError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeSheetUnavailableFormatError as error:
        return jsonify({"error": str(error)}), 501

    return jsonify(table.to_dict()), 200


DOC_EXPORT_FORMATS = ("pdf", "docx", "md")


@office_bp.route(f"{PLAY}/docs/<node_id>/export", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def export_doc(node_id):
    """Print/PDF/DOCX/Markdown export — rendered from the JSON content
    model, never from stored HTML (epic D7). Any resolvable access may
    export (reading, not writing)."""
    export_format = (request.args.get("format") or "").lower()
    if export_format not in DOC_EXPORT_FORMATS:
        return (
            jsonify({"error": f"format must be one of {list(DOC_EXPORT_FORMATS)}"}),
            400,
        )

    try:
        data, mimetype, filename = _doc_export_service().export_document(
            g.user.id, node_id, export_format
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeDocContentInvalidError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeDocExportFormatError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeDocExportUnavailableError as error:
        return jsonify({"error": str(error)}), 501

    response = send_file(
        io.BytesIO(data),
        mimetype=mimetype,
        as_attachment=True,
        download_name=sanitize_download_filename(filename),
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# S147-4 — VBWD Spreadsheets. A Sheet is an office_node of doc_type='sheet';
# every route here is reached only through a SESSION, same bar as Docs. The
# edit lease is reused VERBATIM — the sheet grid calls the SAME
# ``/docs/<node_id>/lease``/``/docs/<node_id>/presence`` routes above (one
# home per behaviour; ``OfficeDocEditorService``'s lease methods accept
# doc_type in {text, sheet}), so no lease route is duplicated here.
# ---------------------------------------------------------------------------

SHEET_EXPORT_FORMATS = ("csv", "xlsx", "pdf")  # "pdf" added by the drag/toolbar slice
SHEET_IMPORT_FORMATS = ("csv", "xlsx")


@office_bp.route(f"{PLAY}/sheets", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def create_sheet():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    try:
        view = _sheet_editor_service().create_sheet(
            g.user.id, name, body.get("parent_id")
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeUploadTooLargeError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeQuotaExceededError as error:
        return jsonify({"error": str(error)}), 413

    db.session.commit()
    return jsonify(view.to_dict()), 201


@office_bp.route(f"{PLAY}/sheets/<node_id>", methods=["GET"])
@require_auth
@require_user_permission(USE_PERMISSION)
def get_sheet(node_id):
    """Workbook + computed values. The engine is the authority: every
    formula cell is recalculated before this response is built (requirement
    #3) — the stored cache is never trusted on load."""
    try:
        view = _sheet_editor_service().open_sheet(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeSheetContentInvalidError:
        current_app.logger.error(
            "[office] stored sheet content invalid for node %s", node_id
        )
        return jsonify({"error": "Sheet content is corrupted"}), 500
    except OfficeSheetCeilingExceededError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeContentIntegrityError:
        current_app.logger.error(
            "[office] sheet content integrity check failed for node %s", node_id
        )
        return jsonify({"error": "Content integrity check failed"}), 500

    return jsonify(view.to_dict()), 200


@office_bp.route(f"{PLAY}/sheets/<node_id>/cells", methods=["PUT"])
@require_auth
@require_user_permission(USE_PERMISSION)
def save_sheet_cells(node_id):
    """Recalculates only the dirty sub-graph and returns ONLY the changed
    values (requirement #5), never the whole sheet. ``base_version_no`` is
    the never-trust-the-client guard, same rule as Docs: a stale value is
    rejected with 409 and NOTHING is written.

    ``changes`` may be an empty list ONLY when ``ai_enabled`` is present —
    the owner-only AI-helper toggle (S147-3.5), mirroring how
    ``PUT /docs/<node_id>`` accepts a content-less ``ai_enabled`` flip."""
    body = request.get_json(silent=True) or {}
    changes = body.get("changes", [])
    base_version_no = body.get("base_version_no")
    ai_enabled = body.get("ai_enabled") if "ai_enabled" in body else None
    if not isinstance(changes, list):
        return jsonify({"error": "changes must be a list"}), 400
    if not changes and ai_enabled is None:
        return jsonify({"error": "changes is required"}), 400
    if not isinstance(base_version_no, int):
        return jsonify({"error": "base_version_no is required"}), 400

    try:
        result = _sheet_editor_service().save_cells(
            g.user.id, node_id, changes, base_version_no, ai_enabled=ai_enabled
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareForbiddenError:
        return jsonify({"error": "Forbidden"}), 403
    except OfficeDocLockedError as locked_error:
        return (
            jsonify(
                {
                    "error": "locked",
                    "holder_user_id": str(locked_error.holder_user_id),
                    "expires_at": locked_error.expires_at.isoformat(),
                }
            ),
            409,
        )
    except OfficeDocStaleVersionError as stale_error:
        return (
            jsonify(
                {
                    "error": "stale_version",
                    "current_version_no": stale_error.current_version_no,
                }
            ),
            409,
        )
    except OfficeSheetContentInvalidError as invalid_error:
        return jsonify({"error": str(invalid_error)}), 400
    except OfficeSheetCeilingExceededError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeUploadTooLargeError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeQuotaExceededError as error:
        return jsonify({"error": str(error)}), 413

    db.session.commit()
    return jsonify(result.to_dict()), 200


@office_bp.route(f"{PLAY}/sheets/<node_id>/recalc", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def recalc_sheet(node_id):
    """Force a full recalculation and persist the refreshed cache."""
    try:
        result = _sheet_editor_service().recalc(g.user.id, node_id)
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareForbiddenError:
        return jsonify({"error": "Forbidden"}), 403
    except OfficeDocLockedError as locked_error:
        return (
            jsonify(
                {
                    "error": "locked",
                    "holder_user_id": str(locked_error.holder_user_id),
                    "expires_at": locked_error.expires_at.isoformat(),
                }
            ),
            409,
        )
    except OfficeSheetContentInvalidError as invalid_error:
        return jsonify({"error": str(invalid_error)}), 400
    except OfficeSheetCeilingExceededError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeUploadTooLargeError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeQuotaExceededError as error:
        return jsonify({"error": str(error)}), 413

    db.session.commit()
    return jsonify(result.to_dict()), 200


@office_bp.route(f"{PLAY}/sheets/<node_id>/export", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def export_sheet(node_id):
    export_format = (request.args.get("format") or "").lower()
    if export_format not in SHEET_EXPORT_FORMATS:
        return (
            jsonify({"error": f"format must be one of {list(SHEET_EXPORT_FORMATS)}"}),
            400,
        )

    try:
        data, mimetype, filename = _sheet_editor_service().export_workbook(
            g.user.id, node_id, export_format
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeSheetExportFormatError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeSheetContentInvalidError as invalid_error:
        return jsonify({"error": str(invalid_error)}), 400
    except OfficeSheetUnavailableFormatError as error:
        return jsonify({"error": str(error)}), 501

    response = send_file(
        io.BytesIO(data),
        mimetype=mimetype,
        as_attachment=True,
        download_name=sanitize_download_filename(filename),
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@office_bp.route(f"{PLAY}/sheets/<node_id>/import", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def import_sheet(node_id):
    """Import a .csv/.xlsx file INTO an existing Sheet node as a new
    version. An unmapped Excel function is never silently dropped: it
    imports as its literal source plus a #NAME? cached value, and the
    response's ``unmapped_formulas`` lists every one (requirement #7)."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "file is required"}), 400

    import_format = (request.form.get("format") or "").lower()
    if not import_format:
        import_format = "xlsx" if upload.filename.lower().endswith(".xlsx") else "csv"
    if import_format not in SHEET_IMPORT_FORMATS:
        return (
            jsonify({"error": f"format must be one of {list(SHEET_IMPORT_FORMATS)}"}),
            400,
        )
    data = upload.stream.read()

    try:
        result = _sheet_editor_service().import_workbook(
            g.user.id, node_id, data, import_format
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareForbiddenError:
        return jsonify({"error": "Forbidden"}), 403
    except OfficeSheetImportFormatError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeSheetCeilingExceededError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeSheetUnavailableFormatError as error:
        return jsonify({"error": str(error)}), 501
    except OfficeUploadTooLargeError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeQuotaExceededError as error:
        return jsonify({"error": str(error)}), 413

    db.session.commit()
    return jsonify(result.to_dict()), 200


@office_bp.route(f"{PLAY}/sheets/<node_id>/ai", methods=["POST"])
@require_auth
@require_user_permission(USE_PERMISSION)
def sheet_ai(node_id):
    """The Sheet AI helper (S147-3.5). ``capability`` is the ONLY
    instruction the client may send — a ``prompt`` field is rejected
    outright, before the service layer is even reached, same contract as
    ``doc_ai`` above (one home for that rule, DRY)."""
    body = request.get_json(silent=True) or {}
    if "prompt" in body:
        return (
            jsonify({"error": "Raw prompts are not accepted; use 'capability'"}),
            400,
        )
    capability = body.get("capability")
    if not isinstance(capability, str) or not capability:
        return jsonify({"error": "capability is required"}), 400
    address = body.get("address")
    if not isinstance(address, str) or not address:
        return jsonify({"error": "address is required"}), 400

    try:
        result = _sheet_ai_service().run_capability(
            g.user.id,
            node_id,
            capability,
            address=address,
            range_text=body.get("range"),
            intent=body.get("intent", ""),
        )
    except OfficeNodeNotFoundError:
        return jsonify({"error": "Not found"}), 404
    except OfficeShareForbiddenError:
        return jsonify({"error": "Forbidden"}), 403
    except OfficeAiDisabledError:
        return jsonify({"error": "AI helper is disabled for this document"}), 403
    except OfficeAiInvalidCapabilityError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeSheetContentInvalidError as error:
        return jsonify({"error": str(error)}), 400
    except OfficeSheetCeilingExceededError as error:
        return jsonify({"error": str(error)}), 413
    except OfficeAiBudgetExceededError as error:
        return jsonify({"error": str(error)}), 429
    except OfficeAiProviderError as error:
        current_app.logger.error("[office] sheet AI request failed: %s", error)
        return jsonify({"error": "AI request failed"}), 502

    db.session.commit()
    return jsonify(result), 200


# ---------------------------------------------------------------------------
# Admin catalogue + storage overview (fe-admin `office-admin` plugin, already
# shipped, calls both of these — this closes that gap). Admin RBAC
# (``office.documents.view``), NOT the ``office.use`` access-level permission
# every fe-user route above uses. GDPR: metadata only — see
# ``OfficeAdminService``'s module docstring for exactly what these can never
# expose.
# ---------------------------------------------------------------------------

DOCUMENTS_VIEW_PERMISSION = "office.documents.view"


@office_bp.route("/api/v1/admin/office/documents", methods=["GET"])
@require_auth
@require_admin
@require_permission(DOCUMENTS_VIEW_PERMISSION)
def admin_list_documents():
    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(int(request.args.get("per_page", 20)), 100)

    items, total = _admin_service().list_documents(page, per_page)
    return jsonify(paginate(items, total, page, per_page)), 200


@office_bp.route("/api/v1/admin/office/storage", methods=["GET"])
@require_auth
@require_admin
@require_permission(DOCUMENTS_VIEW_PERMISSION)
def admin_storage_overview():
    return jsonify(_admin_service().storage_overview()), 200
