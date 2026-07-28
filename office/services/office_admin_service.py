"""``OfficeAdminService`` — the admin catalogue/storage read model (S147
gap-fix).

Closes a real scope gap: fe-admin (`vbwd-fe-admin/plugins/office-admin/`,
already shipped) calls two routes S147-1 never defined. Both are read-only
admin surfaces over data the vault already owns — no new tables, no new
domain concept, just two aggregating reads through the existing
repositories (DRY).

GDPR constraint, enforced structurally rather than by convention: this
service returns document METADATA only. Neither method here, nor either
repository query it calls, ever touches ``office_version.storage_key`` or
``office_document.sealed_data_key`` — there is no code path from these
routes to plaintext content, a share token, or anything else that would let
an admin read a user's file through this listing. Owner identification is
the minimum that makes the screen usable: id + email, nothing else from
``UserDetails``.
"""
from __future__ import annotations

from typing import List, Tuple


class OfficeAdminService:
    def __init__(self, document_repository, usage_repository) -> None:
        self._document_repository = document_repository
        self._usage_repository = usage_repository

    def list_documents(self, page: int, per_page: int) -> Tuple[List[dict], int]:
        """Return ``(items, total)`` — ``items`` are plain metadata dicts,
        ready for the core ``paginate()`` envelope."""
        rows, total = self._document_repository.list_for_admin(page, per_page)
        return [row.to_dict() for row in rows], total

    def storage_overview(self) -> dict:
        """``{total_bytes, per_user: [{user_id, email, bytes_used}]}``."""
        per_user_rows = self._usage_repository.list_all_with_owner_email()
        per_user = [
            {"user_id": str(user_id), "email": email, "bytes_used": bytes_used}
            for user_id, email, bytes_used in per_user_rows
        ]
        return {
            "total_bytes": sum(row["bytes_used"] for row in per_user),
            "per_user": per_user,
        }
