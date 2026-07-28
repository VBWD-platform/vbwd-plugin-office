"""``SharingService`` — the share/ACL orchestrator (S147-2).

Owns owner-side share CRUD ("Shared with me", access log) and the public
read/write flows behind ``/api/v1/office/public/<token>``. Every access
decision routes through the ONE ``AccessResolver`` (DRY, epic D5) — this
service never re-implements "is this allowed", it only asks the resolver and
acts on the answer. Content reads/writes delegate to
``OfficeDocumentService``'s share-aware ``*_for_node`` methods so the
envelope-encryption / quota / mime-sniff / attachment-disposition logic
(S147-1, control C1) is never duplicated.

Security controls implemented here (see the sprint doc's table):

* **C2** — an invalid/revoked/expired token all raise the same
  :class:`OfficeShareNotFoundError`; the route layer maps that to one
  indistinguishable 404 (constant-time hash compare in
  :mod:`share_token`; rate limiting is applied at the route).
* **C3** — a password unlock issues a short-lived, share-scoped grant via
  :class:`ShareGrantService` — never a session.
* **C4** — a purely anonymous edit sets ``created_by_share_id`` and leaves
  ``created_by_user_id`` NULL; a logged-in stranger editing via a link is
  still attributed to their own account (we DO know who they are).
* **C5** — every content write's quota is charged to the NODE OWNER
  (``quota_owner_user_id=node.owner_user_id``), never the editor.
* **C6** — every share resolution writes one ``OfficeShareAccess`` row.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from vbwd.utils.datetime_utils import utcnow

from plugins.office.office.models.office_node import NODE_KIND_DOCUMENT, OfficeNode
from plugins.office.office.models.office_share import (
    ALLOWED_SHARE_PERMISSIONS,
    PERMISSION_EDIT,
    OfficeShare,
)
from plugins.office.office.models.office_share_access import (
    ACTION_CONTENT_READ,
    ACTION_CONTENT_WRITE,
    ACTION_RESOLVE,
    ACTION_UNLOCK,
    OfficeShareAccess,
)
from plugins.office.office.services.access_resolver import is_share_live
from plugins.office.office.services.exceptions import (
    OfficeNodeNotFoundError,
    OfficeShareForbiddenError,
    OfficeShareInvalidPermissionError,
    OfficeShareNotFoundError,
    OfficeSharePasswordRequiredError,
    OfficeShareRecipientNotFoundError,
    OfficeShareUnlockFailedError,
)
from plugins.office.office.services.node_access import OfficeNodeAccess
from plugins.office.office.services.share_password import (
    hash_share_password,
    verify_share_password,
)
from plugins.office.office.services.share_token import (
    generate_share_token,
    hash_share_token,
    tokens_match,
)

#: Sentinel distinguishing "field not present in a PATCH body" from an
#: explicit ``None`` (e.g. clearing an expiry or a password).
UNSET = object()


class SharingService:
    def __init__(
        self,
        node_repository,
        document_repository,
        share_repository,
        share_access_repository,
        document_service,
        access_resolver,
        user_repository,
        grant_service,
    ) -> None:
        self._node_repository = node_repository
        self._document_repository = document_repository
        self._share_repository = share_repository
        self._share_access_repository = share_access_repository
        self._document_service = document_service
        self._access_resolver = access_resolver
        self._user_repository = user_repository
        self._grant_service = grant_service
        self._node_access = OfficeNodeAccess(node_repository)

    # ------------------------------------------------------------------
    # Owner-side CRUD
    # ------------------------------------------------------------------

    def list_shares(self, owner_user_id, node_id) -> List[OfficeShare]:
        node = self._node_access.require_owned_node(owner_user_id, node_id)
        return self._share_repository.find_by_node_id(node.id)

    def create_share(
        self,
        owner_user_id,
        node_id,
        *,
        permission: str,
        recipient_email: Optional[str] = None,
        allow_anonymous: bool = False,
        password: Optional[str] = None,
        expires_at=None,
    ) -> Tuple[OfficeShare, str]:
        self._require_valid_permission(permission)
        node = self._node_access.require_owned_node(owner_user_id, node_id)

        subject_user_id = None
        if recipient_email:
            recipient = self._user_repository.find_by_email(recipient_email)
            if recipient is None:
                raise OfficeShareRecipientNotFoundError(recipient_email)
            subject_user_id = recipient.id

        plaintext_token = generate_share_token()
        share = OfficeShare(
            node_id=node.id,
            created_by_user_id=owner_user_id,
            token_hash=hash_share_token(plaintext_token),
            permission=permission,
            subject_user_id=subject_user_id,
            # allow_anonymous only means something for a link share — forced
            # off for a named share so its meaning is never ambiguous.
            allow_anonymous=bool(allow_anonymous) and subject_user_id is None,
            password_hash=hash_share_password(password) if password else None,
            expires_at=expires_at,
        )
        self._share_repository.add(share)
        return share, plaintext_token

    def update_share(
        self,
        owner_user_id,
        share_id,
        *,
        permission=UNSET,
        expires_at=UNSET,
        password=UNSET,
        allow_anonymous=UNSET,
    ) -> OfficeShare:
        share = self._require_owned_share(owner_user_id, share_id)
        if permission is not UNSET:
            self._require_valid_permission(permission)
            share.permission = permission
        if expires_at is not UNSET:
            share.expires_at = expires_at
        if password is not UNSET:
            share.password_hash = hash_share_password(password) if password else None
        if allow_anonymous is not UNSET:
            share.allow_anonymous = bool(allow_anonymous)
        self._share_repository.add(share)
        return share

    def revoke_share(self, owner_user_id, share_id) -> OfficeShare:
        share = self._require_owned_share(owner_user_id, share_id)
        share.revoked_at = utcnow()
        self._share_repository.add(share)
        return share

    def list_shared_with_me(self, user_id) -> List[Tuple[OfficeShare, OfficeNode]]:
        shares = self._share_repository.find_active_named_shares_for_user(user_id)
        results: List[Tuple[OfficeShare, OfficeNode]] = []
        for share in shares:
            node = self._node_repository.find_by_id(share.node_id)
            if node is None or node.trashed_at is not None:
                continue
            results.append((share, node))
        return results

    def access_log(self, owner_user_id, share_id) -> List[OfficeShareAccess]:
        self._require_owned_share(owner_user_id, share_id)
        return self._share_access_repository.find_by_share_id(share_id)

    # ------------------------------------------------------------------
    # "Shared with me" reads — session-only (no token): a named share is
    # identified purely by the recipient's own login (D5's "the platform's
    # existing session auth identifies them"), so these ask AccessResolver
    # WITHOUT a share_token. Kept as separate routes from the S147-1 owner
    # vault routes (which stay strictly owner-only) rather than widening
    # those, so neither surface's contract has to be re-reasoned about; both
    # still go through the ONE AccessResolver underneath (D5's DRY point is
    # about the ACL logic, not the URL).
    # ------------------------------------------------------------------

    def get_shared_document_metadata(self, reader_user_id, node_id) -> dict:
        node, document = self._require_readable_document(reader_user_id, node_id)
        return self._document_service.get_document_metadata_for_node(node, document)

    def get_shared_document_content(self, reader_user_id, node_id, version_no=None):
        node, document = self._require_readable_document(reader_user_id, node_id)
        return self._document_service.get_content_for_node(node, document, version_no)

    def _require_readable_document(self, reader_user_id, node_id):
        access = self._access_resolver.resolve(node_id, user_id=reader_user_id)
        if access is None:
            raise OfficeNodeNotFoundError(node_id)
        node = self._node_repository.find_by_id(node_id)
        if node is None or node.kind != NODE_KIND_DOCUMENT:
            raise OfficeNodeNotFoundError(node_id)
        document = self._document_repository.find_by_node_id(node.id)
        if document is None:
            raise OfficeNodeNotFoundError(node_id)
        return node, document

    def _require_owned_share(self, owner_user_id, share_id) -> OfficeShare:
        share = self._share_repository.find_by_id_for_owner(share_id, owner_user_id)
        if share is None:
            raise OfficeShareNotFoundError(share_id)
        return share

    @staticmethod
    def _require_valid_permission(permission) -> None:
        if permission not in ALLOWED_SHARE_PERMISSIONS:
            raise OfficeShareInvalidPermissionError(
                f"permission must be one of {ALLOWED_SHARE_PERMISSIONS}, got {permission!r}"
            )

    # ------------------------------------------------------------------
    # Public reads/writes
    # ------------------------------------------------------------------

    def get_public_metadata(
        self, token: str, *, user_id=None, ip_hash: Optional[str] = None
    ) -> dict:
        share = self._resolve_active_share_by_token(token)
        access = self._require_access(share, user_id=user_id, share_token=token)
        node = self._node_repository.find_by_id(share.node_id)
        if node is None:
            raise OfficeShareNotFoundError(token)
        document = None
        if node.kind == NODE_KIND_DOCUMENT:
            document = self._document_repository.find_by_node_id(node.id)
        self._audit(share, ACTION_RESOLVE, ip_hash)
        return {
            "node": node,
            "document": document,
            "permission": access,
            "requires_password": share.password_hash is not None,
        }

    def get_public_content(
        self,
        token: str,
        *,
        user_id=None,
        grant_token: Optional[str] = None,
        ip_hash: Optional[str] = None,
    ):
        share = self._resolve_active_share_by_token(token)
        self._require_access(share, user_id=user_id, share_token=token)
        self._require_unlocked(share, grant_token)
        node, document = self._require_document_node(share)
        result = self._document_service.get_content_for_node(node, document)
        self._audit(share, ACTION_CONTENT_READ, ip_hash)
        return result

    def put_public_content(
        self,
        token: str,
        data: bytes,
        *,
        user_id=None,
        grant_token: Optional[str] = None,
        ip_hash: Optional[str] = None,
    ):
        share = self._resolve_active_share_by_token(token)
        access = self._require_access(share, user_id=user_id, share_token=token)
        if access != PERMISSION_EDIT:
            raise OfficeShareForbiddenError(token)
        self._require_unlocked(share, grant_token)
        node, document = self._require_document_node(share)

        # C4: a fully anonymous editor (no user_id at all) is attributed to
        # the share, never a user; a logged-in stranger via a link keeps
        # their own identity — we DO know who they were.
        created_by_share_id = share.id if user_id is None else None
        version = self._document_service.add_version_for_node(
            node,
            document,
            data,
            actor_user_id=user_id,
            created_by_share_id=created_by_share_id,
            quota_owner_user_id=node.owner_user_id,  # C5
        )
        self._audit(share, ACTION_CONTENT_WRITE, ip_hash)
        return version

    def unlock_share(
        self, token: str, password: str, *, ip_hash: Optional[str] = None
    ) -> str:
        share = self._resolve_active_share_by_token(token)
        if share.password_hash is None:
            raise OfficeSharePasswordRequiredError(token)
        if not verify_share_password(password or "", share.password_hash):
            raise OfficeShareUnlockFailedError(token)
        self._audit(share, ACTION_UNLOCK, ip_hash)
        return self._grant_service.issue(share.id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_active_share_by_token(self, token: str) -> OfficeShare:
        """C2: an invalid, revoked, or expired token all raise the exact
        same error — indistinguishable from "never existed"."""
        if not token:
            raise OfficeShareNotFoundError(token)
        presented_hash = hash_share_token(token)
        share = self._share_repository.find_by_token_hash(presented_hash)
        if share is None or not tokens_match(presented_hash, share.token_hash):
            raise OfficeShareNotFoundError(token)
        if not is_share_live(share):
            raise OfficeShareNotFoundError(token)
        return share

    def _require_access(self, share: OfficeShare, *, user_id, share_token) -> str:
        access = self._access_resolver.resolve(
            share.node_id, user_id=user_id, share_token=share_token
        )
        if access is None:
            raise OfficeShareNotFoundError(share_token)
        return access

    def _require_unlocked(self, share: OfficeShare, grant_token: Optional[str]) -> None:
        if share.password_hash is None:
            return
        if not self._grant_service.verify(grant_token, share.id):
            raise OfficeSharePasswordRequiredError(str(share.id))

    def _require_document_node(self, share: OfficeShare):
        node = self._node_repository.find_by_id(share.node_id)
        if node is None or node.kind != NODE_KIND_DOCUMENT:
            raise OfficeShareNotFoundError(str(share.id))
        document = self._document_repository.find_by_node_id(node.id)
        if document is None:
            raise OfficeShareNotFoundError(str(share.id))
        return node, document

    def _audit(self, share: OfficeShare, action: str, ip_hash: Optional[str]) -> None:
        share.last_used_at = utcnow()
        self._share_repository.add(share)
        self._share_access_repository.add(
            OfficeShareAccess(share_id=share.id, action=action, ip_hash=ip_hash)
        )
