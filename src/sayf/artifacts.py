from __future__ import annotations

import ctypes
import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ConfigDict

from sayf.hashing import HASH_PREFIX
from sayf.records import ArtifactDescriptor

_CHUNK_SIZE = 1024 * 1024
_MOVEFILE_WRITE_THROUGH = 0x00000008


class ArtifactStoreError(RuntimeError):
    """Raised when immutable artifact storage cannot be used safely."""


class ArtifactVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    digest: str
    valid: bool
    size_bytes: int | None = None
    reason: str | None = None


class ContentAddressedArtifactStore:
    """Filesystem SHA-256 content-addressed store with verification on read."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @staticmethod
    def _validated_digest(digest: str) -> str:
        return ArtifactDescriptor(digest=digest, size_bytes=0).digest

    def path_for(self, digest: str) -> Path:
        digest = self._validated_digest(digest)
        hex_digest = digest.removeprefix(HASH_PREFIX)
        return self.root / "sha256" / hex_digest[:2] / hex_digest

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _windows_move_write_through(source: Path, destination: Path) -> None:
        if os.name != "nt":
            raise OSError("Windows write-through move requested on non-Windows platform")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file_ex = kernel32.MoveFileExW
        move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move_file_ex.restype = ctypes.c_int
        if move_file_ex(str(source), str(destination), _MOVEFILE_WRITE_THROUGH):
            return
        error_code = ctypes.get_last_error()
        detail = ctypes.FormatError(error_code).strip()
        raise OSError(error_code, f"MoveFileExW failed: {detail}")

    @staticmethod
    def _copy_and_hash(source: BinaryIO, destination: BinaryIO) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        while True:
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            destination.write(chunk)
            size += len(chunk)
        return f"{HASH_PREFIX}{hasher.hexdigest()}", size

    def _verify_path(self, path: Path, expected_digest: str) -> ArtifactVerification:
        if path.is_symlink():
            return ArtifactVerification(
                digest=expected_digest,
                valid=False,
                reason="artifact object path must not be a symbolic link",
            )
        if not path.exists():
            return ArtifactVerification(
                digest=expected_digest,
                valid=False,
                reason="artifact object does not exist",
            )
        if not path.is_file():
            return ArtifactVerification(
                digest=expected_digest,
                valid=False,
                reason="artifact object path is not a file",
            )

        hasher = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as artifact:
                while True:
                    chunk = artifact.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            return ArtifactVerification(
                digest=expected_digest,
                valid=False,
                reason=f"unable to read artifact object: {exc}",
            )

        actual = f"{HASH_PREFIX}{hasher.hexdigest()}"
        if actual != expected_digest:
            return ArtifactVerification(
                digest=expected_digest,
                valid=False,
                size_bytes=size,
                reason=f"artifact digest mismatch: found {actual}",
            )
        return ArtifactVerification(digest=expected_digest, valid=True, size_bytes=size)

    def verify(self, digest: str) -> ArtifactVerification:
        digest = self._validated_digest(digest)
        return self._verify_path(self.path_for(digest), digest)

    def _publish_staging(self, staging_path: Path, digest: str) -> int:
        target = self.path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)

        existing = self._verify_path(target, digest)
        if target.exists() or target.is_symlink():
            if not existing.valid:
                raise ArtifactStoreError(
                    f"refusing to replace invalid existing artifact object {digest}: "
                    f"{existing.reason}"
                )
            staging_path.unlink(missing_ok=True)
            return existing.size_bytes or 0

        try:
            if os.name == "nt":
                try:
                    self._windows_move_write_through(staging_path, target)
                except OSError as exc:
                    if target.exists():
                        concurrent = self._verify_path(target, digest)
                        if concurrent.valid:
                            staging_path.unlink(missing_ok=True)
                            return concurrent.size_bytes or 0
                    raise exc
            else:
                os.replace(staging_path, target)
                self._fsync_directory(target.parent)
        except OSError as exc:
            raise ArtifactStoreError(f"unable to publish artifact object {digest}: {exc}") from exc

        verification = self._verify_path(target, digest)
        if not verification.valid:
            raise ArtifactStoreError(
                f"published artifact object failed verification {digest}: {verification.reason}"
            )
        return verification.size_bytes or 0

    def put_bytes(
        self,
        content: bytes,
        *,
        media_type: str | None = None,
        name: str | None = None,
        metadata: dict | None = None,
    ) -> ArtifactDescriptor:
        if not isinstance(content, bytes):
            raise TypeError("artifact content must be bytes")
        options = ArtifactDescriptor(
            digest=f"{HASH_PREFIX}{'0' * 64}",
            size_bytes=0,
            media_type=media_type,
            name=name,
            metadata={} if metadata is None else metadata,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        staging_path: Path | None = None
        try:
            fd, staging_name = tempfile.mkstemp(prefix=".sayf-artifact-", dir=self.root)
            staging_path = Path(staging_name)
            with os.fdopen(fd, "wb") as staging:
                staging.write(content)
                staging.flush()
                os.fsync(staging.fileno())
            digest = f"{HASH_PREFIX}{hashlib.sha256(content).hexdigest()}"
            size = self._publish_staging(staging_path, digest)
            staging_path = None
            return ArtifactDescriptor(
                digest=digest,
                size_bytes=size,
                media_type=options.media_type,
                name=options.name,
                metadata=options.metadata,
            )
        except ArtifactStoreError:
            raise
        except OSError as exc:
            raise ArtifactStoreError(f"unable to write artifact object: {exc}") from exc
        finally:
            if staging_path is not None:
                try:
                    staging_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def put_file(
        self,
        source: str | Path,
        *,
        media_type: str | None = None,
        name: str | None = None,
        metadata: dict | None = None,
    ) -> ArtifactDescriptor:
        source_path = Path(source)
        if source_path.is_symlink() or not source_path.is_file():
            raise ArtifactStoreError("artifact source must be a regular file, not a symlink")
        options = ArtifactDescriptor(
            digest=f"{HASH_PREFIX}{'0' * 64}",
            size_bytes=0,
            media_type=media_type,
            name=source_path.name if name is None else name,
            metadata={} if metadata is None else metadata,
        )

        self.root.mkdir(parents=True, exist_ok=True)
        staging_path: Path | None = None
        try:
            fd, staging_name = tempfile.mkstemp(prefix=".sayf-artifact-", dir=self.root)
            staging_path = Path(staging_name)
            with source_path.open("rb") as source_file, os.fdopen(fd, "wb") as staging:
                digest, staged_size = self._copy_and_hash(source_file, staging)
                staging.flush()
                os.fsync(staging.fileno())
            published_size = self._publish_staging(staging_path, digest)
            staging_path = None
            if staged_size != published_size:
                raise ArtifactStoreError(
                    "artifact size changed during publication or concurrent object reuse"
                )
            return ArtifactDescriptor(
                digest=digest,
                size_bytes=published_size,
                media_type=options.media_type,
                name=options.name,
                metadata=options.metadata,
            )
        except ArtifactStoreError:
            raise
        except OSError as exc:
            raise ArtifactStoreError(f"unable to store artifact file: {exc}") from exc
        finally:
            if staging_path is not None:
                try:
                    staging_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def read_bytes(self, digest: str) -> bytes:
        digest = self._validated_digest(digest)
        path = self.path_for(digest)
        if path.is_symlink() or not path.is_file():
            raise ArtifactStoreError(f"artifact object {digest} is not a regular file")
        hasher = hashlib.sha256()
        content = bytearray()
        try:
            with path.open("rb") as artifact:
                while True:
                    chunk = artifact.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    content.extend(chunk)
        except OSError as exc:
            raise ArtifactStoreError(f"unable to read artifact object {digest}: {exc}") from exc
        actual = f"{HASH_PREFIX}{hasher.hexdigest()}"
        if actual != digest:
            raise ArtifactStoreError(
                f"artifact object {digest} failed read verification: found {actual}"
            )
        return bytes(content)
