"""Pluggable blob storage for raw uploaded document bytes.

Where the original bytes of an upload live is an operator decision — Postgres (default; one
backup domain, one RLS perimeter), a mounted directory (cheap and large), S3/MinIO (durable and
offsite), or nowhere at all when policy forbids retention. Callers do not care: they take a
:class:`BlobStore` from :func:`get_blob_store` and speak the same four verbs to all of them.

Typical use::

    from di.storage import blob_key, get_blob_store

    store = get_blob_store()
    key = blob_key(client_id, sha256_hex(data), filename)
    ref = await store.put(client_id=client_id, key=key, data=data, content_type="application/pdf")
    # persist ref.uri alongside the document row; later:
    data = await store.get(ref.uri, client_id=client_id)

The backend is chosen once from ``settings.blob_backend`` and cached;
:func:`reset_blob_store` clears the cache (tests, or a settings reload).
"""
from __future__ import annotations

from di.config import Settings, get_settings
from di.storage.base import (
    BACKEND_LOCAL,
    BACKEND_NONE,
    BACKEND_POSTGRES,
    BACKEND_S3,
    BlobNotFound,
    BlobRef,
    BlobStore,
    BlobStoreError,
    NoneBlobStore,
    blob_key,
    sha256_hex,
)
from di.storage.local import LocalBlobStore
from di.storage.postgres import PostgresBlobStore
from di.storage.s3 import S3BlobStore

__all__ = [
    "BACKEND_LOCAL",
    "BACKEND_NONE",
    "BACKEND_POSTGRES",
    "BACKEND_S3",
    "BlobNotFound",
    "BlobRef",
    "BlobStore",
    "BlobStoreError",
    "LocalBlobStore",
    "NoneBlobStore",
    "PostgresBlobStore",
    "S3BlobStore",
    "blob_key",
    "get_blob_store",
    "reset_blob_store",
    "sha256_hex",
]

_store: BlobStore | None = None


def _build_store(settings: Settings) -> BlobStore:
    """Instantiate the backend named by ``settings.blob_backend``.

    Args:
        settings: Live settings.

    Returns:
        A ready :class:`BlobStore` (no I/O has happened yet).

    Raises:
        BlobStoreError: ``blob_backend`` is not a known backend.
    """
    backend = settings.blob_backend
    if backend == BACKEND_POSTGRES:
        return PostgresBlobStore()
    if backend == BACKEND_LOCAL:
        return LocalBlobStore(settings.blob_local_dir)
    if backend == BACKEND_S3:
        return S3BlobStore(
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
            endpoint=settings.s3_endpoint,
            region=settings.s3_region,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
        )
    if backend == BACKEND_NONE:
        return NoneBlobStore()
    raise BlobStoreError(f"unknown blob_backend: {backend!r}")


def get_blob_store(settings: Settings | None = None) -> BlobStore:
    """Return the process-wide blob store, building it on first call.

    Args:
        settings: Settings to build from; defaults to :func:`di.config.get_settings`. Ignored
            once a store is cached — call :func:`reset_blob_store` first to rebuild.

    Returns:
        The cached :class:`BlobStore` singleton.

    Raises:
        BlobStoreError: ``blob_backend`` is not a known backend.
    """
    global _store
    if _store is None:
        _store = _build_store(settings or get_settings())
    return _store


def reset_blob_store() -> None:
    """Drop the cached store so the next :func:`get_blob_store` rebuilds it (test hook)."""
    global _store
    _store = None
