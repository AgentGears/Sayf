import hashlib
from pathlib import Path

import pytest

from sayf.artifacts import ArtifactStoreError, ContentAddressedArtifactStore


def test_put_verify_and_read_bytes(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    content = b"sayf artifact\n"
    descriptor = store.put_bytes(content, media_type="text/plain", name="demo.txt")

    expected = "sha256:" + hashlib.sha256(content).hexdigest()
    assert descriptor.digest == expected
    assert descriptor.size_bytes == len(content)
    assert store.verify(expected).valid is True
    assert store.read_bytes(expected) == content


def test_same_content_reuses_same_address(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    first = store.put_bytes(b"same")
    second = store.put_bytes(b"same")
    assert first.digest == second.digest
    assert store.path_for(first.digest) == store.path_for(second.digest)


def test_existing_corrupt_object_is_not_silently_replaced(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    descriptor = store.put_bytes(b"expected")
    object_path = store.path_for(descriptor.digest)
    object_path.write_bytes(b"corrupt")

    verification = store.verify(descriptor.digest)
    assert verification.valid is False
    assert "digest mismatch" in (verification.reason or "")

    with pytest.raises(ArtifactStoreError, match="refusing to replace"):
        store.put_bytes(b"expected")
    assert object_path.read_bytes() == b"corrupt"


def test_put_file_rejects_symlink_source(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"data")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlink creation unavailable")

    store = ContentAddressedArtifactStore(tmp_path / "objects")
    with pytest.raises(ArtifactStoreError, match="regular file"):
        store.put_file(link)


def test_digest_cannot_escape_object_root(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    with pytest.raises(ValueError):
        store.path_for("sha256:../../etc/passwd")
