"""``DocumentStore`` — the single home for read/write/delete and envelope
encryption over the office plugin's filespace (S147-1).

Per the epic's D3, plugin filespaces are unencrypted at rest and the core
cipher's decrypt path decodes UTF-8 (``vbwd/utils/crypto.py``), so it cannot
carry binary blobs even if a namespace flipped ``encrypt=True`` on. This store
implements envelope encryption itself, over raw bytes, so it is binary-safe:

* a random per-document **data key** (a fresh Fernet key) is generated once,
  at document creation, and *sealed* (encrypted) with the app secret via the
  core :func:`vbwd.utils.crypto.get_default_cipher` — the sealed key text is
  ASCII-safe, so it round-trips through the core cipher's UTF-8 path fine and
  is stored on ``OfficeDocument.sealed_data_key``;
* document **content** is sealed with the per-document data key directly via
  ``cryptography.fernet.Fernet`` (bytes in, bytes out — never decoded as
  text), so binary payloads round-trip byte-identical.

Nothing else in the plugin touches ``filesystem_manager.for_plugin("office")``
directly — every read/write/delete of document bytes goes through this class.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from vbwd.utils.crypto import get_default_cipher

from .exceptions import OfficeContentIntegrityError


@dataclass(frozen=True)
class WrittenBlob:
    """The result of persisting one version's plaintext bytes.

    ``sha256`` is the digest of the PLAINTEXT (computed once here, at write
    time) so a read-back can be verified without re-deriving it (B5).
    """

    storage_key: str
    size_bytes: int
    sha256: str


class DocumentStore:
    """Read/write/delete + envelope seal/open for one document's bytes."""

    def __init__(self, filespace) -> None:
        """``filespace`` is a ``PluginFilespace`` bound to the office
        namespace (``filesystem_manager.for_plugin("office")``), or the
        in-memory test double with the same contract."""
        self._filespace = filespace

    @staticmethod
    def generate_sealed_data_key() -> str:
        """Create a fresh per-document data key, sealed with the app secret.

        The raw key never leaves this function unsealed — callers only ever
        hold the sealed text (persisted on ``OfficeDocument.sealed_data_key``).
        """
        raw_key = Fernet.generate_key()  # URL-safe base64, ASCII bytes
        return get_default_cipher().encrypt(raw_key.decode("ascii"))

    @staticmethod
    def _open_data_key(sealed_data_key: str) -> bytes:
        return get_default_cipher().decrypt(sealed_data_key).encode("ascii")

    @staticmethod
    def build_storage_key(owner_user_id, document_id, version_no: int) -> str:
        """``<owner_user_id>/<document_id>/<version_no>`` (S147-1 spec,
        "Storage"). Every segment is a server-minted id/int — never a
        client-supplied filename — so there is no traversal surface here;
        the core ``FilesystemManager`` still confines the resolved path as
        defence in depth."""
        return f"{owner_user_id}/{document_id}/{version_no}"

    def write_new_version(
        self,
        owner_user_id,
        document_id,
        version_no: int,
        sealed_data_key: str,
        data: bytes,
    ) -> WrittenBlob:
        """Seal ``data`` under the document's data key and persist it at a
        FRESH storage key. Only writes mint a new storage key — a restored
        version reuses an existing one (see the version-history service)."""
        storage_key = self.build_storage_key(owner_user_id, document_id, version_no)
        fernet = Fernet(self._open_data_key(sealed_data_key))
        ciphertext = fernet.encrypt(data)
        self._filespace.write_bytes(storage_key, ciphertext)
        return WrittenBlob(
            storage_key=storage_key,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def read(
        self, storage_key: str, sealed_data_key: str, expected_sha256: str
    ) -> bytes:
        """Open the blob at ``storage_key`` and verify it against
        ``expected_sha256`` (B5) — a mismatch raises, never a silent
        fallback."""
        ciphertext = self._filespace.read_bytes(storage_key)
        fernet = Fernet(self._open_data_key(sealed_data_key))
        try:
            plaintext = fernet.decrypt(ciphertext)
        except InvalidToken as tamper_error:
            raise OfficeContentIntegrityError(
                f"Content at '{storage_key}' failed to decrypt (tampered or "
                "wrong key)"
            ) from tamper_error

        actual_sha256 = hashlib.sha256(plaintext).hexdigest()
        if actual_sha256 != expected_sha256:
            raise OfficeContentIntegrityError(
                f"sha256 mismatch reading '{storage_key}': expected "
                f"{expected_sha256}, got {actual_sha256}"
            )
        return plaintext

    def delete(self, storage_key: str) -> None:
        self._filespace.delete(storage_key)
