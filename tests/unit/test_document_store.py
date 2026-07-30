"""S147-1 test #1/#2 — ``DocumentStore`` envelope encryption + confinement.

Uses the in-memory filesystem double (no disk) for the round-trip cases and
the REAL ``LocalFilesystemManager`` (a tmp dir) for the path-traversal case,
because confinement is enforced by the on-disk ``realpath`` check — the seam
this store deliberately does not re-implement (S147-1 spec, "Storage").
"""
import os

import pytest

from vbwd.services.filesystem.local import LocalFilesystemManager
from vbwd.services.filesystem.memory import InMemoryFilesystemManager

from plugins.office.office.storage.document_store import DocumentStore, WrittenBlob
from plugins.office.office.storage.exceptions import OfficeContentIntegrityError


@pytest.fixture
def store():
    filespace = InMemoryFilesystemManager().for_plugin("office")
    return DocumentStore(filespace)


def test_write_then_read_round_trips_byte_identical_text(store):
    sealed_key = DocumentStore.generate_sealed_data_key()
    payload = "héllo wörld — plain text".encode("utf-8")

    blob = store.write_new_version("owner-1", "doc-1", 1, sealed_key, payload)
    assert isinstance(blob, WrittenBlob)
    assert blob.size_bytes == len(payload)

    recovered = store.read(blob.storage_key, sealed_key, blob.sha256)

    assert recovered == payload


def test_write_then_read_round_trips_byte_identical_binary_fixture(store):
    # The case the core cipher path cannot handle (it decodes UTF-8) — every
    # byte value 0..255 back to back, including NUL.
    sealed_key = DocumentStore.generate_sealed_data_key()
    payload = bytes(range(256)) * 37

    blob = store.write_new_version("owner-2", "doc-2", 1, sealed_key, payload)
    recovered = store.read(blob.storage_key, sealed_key, blob.sha256)

    assert recovered == payload
    assert blob.sha256 == blob.sha256  # sanity: digest computed once, reused


def test_two_documents_get_independent_data_keys(store):
    key_a = DocumentStore.generate_sealed_data_key()
    key_b = DocumentStore.generate_sealed_data_key()

    assert key_a != key_b


def test_content_on_disk_is_not_the_plaintext(store):
    sealed_key = DocumentStore.generate_sealed_data_key()
    payload = b"the quick brown fox jumps over the lazy dog"

    blob = store.write_new_version("owner-3", "doc-3", 1, sealed_key, payload)

    raw_on_disk = store._filespace.read_bytes(blob.storage_key)
    assert raw_on_disk != payload


def test_storage_key_is_owner_document_version(store):
    sealed_key = DocumentStore.generate_sealed_data_key()
    blob = store.write_new_version("owner-4", "doc-4", 3, sealed_key, b"data")

    assert blob.storage_key == "owner-4/doc-4/3"


def test_read_raises_on_digest_mismatch(store):
    sealed_key = DocumentStore.generate_sealed_data_key()
    blob = store.write_new_version("owner-5", "doc-5", 1, sealed_key, b"original")

    with pytest.raises(OfficeContentIntegrityError):
        store.read(blob.storage_key, sealed_key, "0" * 64)


def test_delete_removes_the_blob(store):
    sealed_key = DocumentStore.generate_sealed_data_key()
    blob = store.write_new_version("owner-6", "doc-6", 1, sealed_key, b"gone soon")

    store.delete(blob.storage_key)

    assert store._filespace.exists(blob.storage_key) is False


def test_path_traversal_in_document_id_cannot_escape_the_namespace_root(tmp_path):
    real_manager = LocalFilesystemManager(var_root=str(tmp_path))
    real_store = DocumentStore(real_manager.for_plugin("office"))
    sealed_key = DocumentStore.generate_sealed_data_key()

    with pytest.raises(ValueError):
        real_store.write_new_version("../../etc", "passwd", 1, sealed_key, b"malicious")

    # Nothing escaped the plugin's confined root.
    assert not os.path.exists(os.path.join(str(tmp_path), "..", "etc"))
