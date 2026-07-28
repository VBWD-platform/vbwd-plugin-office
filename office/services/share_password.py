"""Optional share password (S147-2 control C3).

Bcrypt, mirroring ``vbwd.services.auth_service.AuthService.hash_password`` —
unlike the share token (high-entropy, single fast hash is fine), a share
password may be low-entropy and human-chosen, so it gets the slow KDF a
password deserves.
"""
import bcrypt


def hash_share_password(password: str) -> str:
    """Bcrypt-hash a share password for storage on ``password_hash``."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_share_password(password: str, password_hash: str) -> bool:
    """True iff ``password`` matches the stored bcrypt ``password_hash``."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False
