"""Share-link token generation/hashing (S147-2 epic D5).

Mirrors ``vbwd.services.api_key_service.ApiKeyService`` exactly (S52): the
plaintext is high-entropy and shown to the owner exactly once, only its
sha256 hash is ever persisted, and a presented token is compared against the
stored hash in constant time. A leaked database dump must not hand over a
live capability — the same reasoning as password storage, except the token's
own entropy already makes a single fast hash appropriate (this is not a
low-entropy secret needing a slow KDF).
"""
import hashlib
import hmac
import secrets

SHARE_TOKEN_PREFIX = "vbwds_"
SHARE_TOKEN_ENTROPY_BYTES = 32


def generate_share_token() -> str:
    """A fresh, high-entropy, URL-safe plaintext share token."""
    return SHARE_TOKEN_PREFIX + secrets.token_urlsafe(SHARE_TOKEN_ENTROPY_BYTES)


def hash_share_token(plaintext: str) -> str:
    """The sha256 hex digest stored as ``OfficeShare.token_hash``."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def tokens_match(candidate_hash: str, stored_hash: str) -> bool:
    """Constant-time comparison of two token hashes (C2)."""
    return hmac.compare_digest(candidate_hash, stored_hash)
