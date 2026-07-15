"""Filesystem blob backend — a mounted directory (docker volume, PVC, NFS share).

Layout, rooted at ``settings.blob_local_dir``::

    <base>/<sha256(client_id)[:32]>/<h[:2]>/<h[2:4]>/<h>        # payload, h = sha256(key)
    <base>/<sha256(client_id)[:32]>/<h[:2]>/<h[2:4]>/<h>.json   # sidecar metadata

**The key is never used as a path.** The on-disk name is a digest of the key, so a hostile key
(``../../etc/passwd``, a NUL byte, an absolute path) is structurally incapable of escaping the
tenant's directory — traversal is neutralized by construction rather than by blacklist. Every
resolved path is then re-checked with ``Path.resolve().relative_to(base)`` as defence in depth,
which also catches symlinks planted inside the volume. Reads additionally verify the path sits
under the *calling* tenant's directory, so a leaked ``file://`` URI is not a cross-tenant read.

The original key, content type and size are unrecoverable from a digest, so each payload gets a
small JSON sidecar for operators and forensics.

All filesystem calls are blocking and run in a worker thread via ``anyio.to_thread.run_sync``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from anyio import to_thread

from di.storage.base import (
    BACKEND_LOCAL,
    BlobNotFound,
    BlobRef,
    BlobStore,
    BlobStoreError,
    sha256_hex,
)

logger = logging.getLogger(__name__)

_URI_SCHEME = "file://"


class LocalBlobStore(BlobStore):
    """Store blobs as files under a base directory.

    Args:
        base_dir: Root directory for all tenants (``settings.blob_local_dir``). Created on
            demand; need not exist at construction time.
    """

    backend = BACKEND_LOCAL

    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir).expanduser().resolve()

    # -- path derivation ---------------------------------------------------
    def _client_dir(self, client_id: str) -> Path:
        """Return the tenant's directory: a digest of ``client_id``, so any id is path-safe."""
        return self._base / hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:32]

    def _blob_path(self, client_id: str, key: str) -> Path:
        """Map ``(client_id, key)`` to its sharded on-disk path, verified inside the base dir."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = self._client_dir(client_id) / digest[:2] / digest[2:4] / digest
        return self._verify_inside(path, self._base)

    def _verify_inside(self, path: Path, root: Path) -> Path:
        """Resolve ``path`` and assert it lives under ``root``.

        Args:
            path: Candidate path (may not exist).
            root: Directory the path must be contained in.

        Returns:
            The resolved path.

        Raises:
            BlobStoreError: The resolved path escapes ``root``.
        """
        resolved = path.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise BlobStoreError(f"blob path escapes {root}: {path}") from exc
        return resolved

    def _path_from_uri(self, uri: str, client_id: str) -> Path:
        """Parse a ``file://`` URI and confirm it belongs to ``client_id``.

        Raises:
            BlobStoreError: Wrong scheme, or the path is outside the tenant's directory.
        """
        if not uri.startswith(_URI_SCHEME):
            raise BlobStoreError(f"not a local blob uri: {uri!r}")
        path = Path(uri[len(_URI_SCHEME) :])
        if not path.is_absolute():
            raise BlobStoreError(f"local blob uri must be absolute: {uri!r}")
        return self._verify_inside(path, self._client_dir(client_id))

    # -- blocking helpers (run in a worker thread) -------------------------
    @staticmethod
    def _write_sync(path: Path, data: bytes, sidecar: dict[str, Any]) -> None:
        """Write payload + sidecar atomically (temp file then rename)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        sidecar_tmp = path.with_name(path.name + ".json.tmp")
        sidecar_tmp.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
        os.replace(sidecar_tmp, path.with_name(path.name + ".json"))

    @staticmethod
    def _read_sync(path: Path) -> bytes:
        return path.read_bytes()

    @staticmethod
    def _delete_sync(path: Path) -> bool:
        """Remove payload + sidecar; return whether the payload existed."""
        existed = path.is_file()
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".json").unlink(missing_ok=True)
        return existed

    @staticmethod
    def _purge_sync(client_dir: Path) -> int:
        """Count payloads under ``client_dir``, then remove the tree."""
        if not client_dir.is_dir():
            return 0
        count = sum(
            1
            for p in client_dir.rglob("*")
            if p.is_file() and not p.name.endswith((".json", ".tmp"))
        )
        shutil.rmtree(client_dir)
        return count

    def _health_sync(self) -> dict[str, Any]:
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            if not os.access(self._base, os.W_OK):
                return {
                    "backend": self.backend,
                    "ok": False,
                    "detail": f"{self._base} is not writable",
                }
            return {"backend": self.backend, "ok": True, "detail": f"base={self._base}"}
        except OSError as exc:
            return {"backend": self.backend, "ok": False, "detail": str(exc)}

    # -- BlobStore ---------------------------------------------------------
    async def put(
        self, *, client_id: str, key: str, data: bytes, content_type: str | None = None
    ) -> BlobRef:
        """Write ``data`` to the tenant's tree, overwriting any blob with the same key."""
        path = self._blob_path(client_id, key)
        digest = sha256_hex(data)
        sidecar = {
            "client_id": client_id,
            "key": key,
            "content_type": content_type,
            "size": len(data),
            "sha256": digest,
            "created_at": datetime.now(UTC).isoformat(),
        }
        try:
            await to_thread.run_sync(partial(self._write_sync, path, data, sidecar))
        except OSError as exc:
            raise BlobStoreError(f"local blob write failed: {exc}") from exc
        return BlobRef(
            uri=f"{_URI_SCHEME}{path}",
            backend=self.backend,
            size=len(data),
            content_type=content_type,
            sha256=digest,
        )

    async def get(self, uri: str, *, client_id: str) -> bytes:
        """Read the bytes at ``uri``, refusing paths outside the tenant's directory."""
        path = self._path_from_uri(uri, client_id)
        try:
            return await to_thread.run_sync(partial(self._read_sync, path))
        except FileNotFoundError as exc:
            raise BlobNotFound(f"no blob at {uri}") from exc
        except OSError as exc:
            raise BlobStoreError(f"local blob read failed: {exc}") from exc

    async def delete(self, uri: str, *, client_id: str) -> bool:
        """Remove the blob at ``uri`` and its sidecar."""
        path = self._path_from_uri(uri, client_id)
        try:
            return await to_thread.run_sync(partial(self._delete_sync, path))
        except OSError as exc:
            raise BlobStoreError(f"local blob delete failed: {exc}") from exc

    async def delete_client(self, client_id: str) -> int:
        """Remove the tenant's entire directory."""
        client_dir = self._verify_inside(self._client_dir(client_id), self._base)
        try:
            return await to_thread.run_sync(partial(self._purge_sync, client_dir))
        except OSError as exc:
            raise BlobStoreError(f"local blob purge failed: {exc}") from exc

    async def health(self) -> dict[str, Any]:
        """Check the base directory exists and is writable."""
        return await to_thread.run_sync(self._health_sync)
