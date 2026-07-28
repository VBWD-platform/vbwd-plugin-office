"""``ShareGrantService`` — the C3 password-unlock grant (S147-2).

A password-protected share must never widen into a session (D5) — unlocking
it authorises exactly that one share, for a short time, and nothing else
(C3). The grant is a small signed/encrypted envelope (share id + expiry)
built with the CORE symmetric cipher (``vbwd.utils.crypto.get_default_cipher``
— the same seam ``DocumentStore`` uses to seal per-document data keys), not a
bespoke crypto scheme: one cipher, reused, per DRY. The server holds no
server-side grant state (no table, no cache to forget to invalidate) — the
grant is self-contained and self-expiring, and ``verify`` re-checks the
requested share id on every use so a grant for share A can never unlock
share B (test #8's "the grant does not open a different share").
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from cryptography.fernet import InvalidToken

from vbwd.utils.crypto import get_default_cipher
from vbwd.utils.datetime_utils import utcnow

#: How long a password unlock stays valid before the visitor must re-enter
#: the password. Short by design (C3 — a capability grant, not a session).
GRANT_TTL_SECONDS = 900


class ShareGrantService:
    """Issue and verify short-lived, share-scoped unlock grants."""

    def issue(self, share_id) -> str:
        expires_at = utcnow() + timedelta(seconds=GRANT_TTL_SECONDS)
        payload = {"share_id": str(share_id), "expires_at": expires_at.isoformat()}
        return get_default_cipher().encrypt(json.dumps(payload))

    def verify(self, grant_token: Optional[str], share_id) -> bool:
        if not grant_token:
            return False
        try:
            payload = json.loads(get_default_cipher().decrypt(grant_token))
        except (InvalidToken, ValueError):
            return False
        if payload.get("share_id") != str(share_id):
            return False
        try:
            expires_at = datetime.fromisoformat(payload.get("expires_at", ""))
        except (TypeError, ValueError):
            return False
        return expires_at >= utcnow()
