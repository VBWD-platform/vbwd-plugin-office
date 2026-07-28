"""Client-IP hashing for the C6 audit trail (S147-2).

An IP hash rather than the raw address keeps ``office_share_access``
proportionate (GDPR-conscious): enough to spot abuse patterns on one
share's link without storing a directly-identifying value.
"""
import hashlib
from typing import Optional


def hash_client_ip(raw_ip: Optional[str]) -> Optional[str]:
    if not raw_ip:
        return None
    return hashlib.sha256(raw_ip.encode("utf-8")).hexdigest()
