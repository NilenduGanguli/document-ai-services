"""Blob-storage contracts: value types, the :class:`BlobStore` interface, and key derivation.

Raw uploaded bytes are the one artefact in the platform whose retention policy is an *operator*
decision, not a product decision: some deployments want them in Postgres (single backup domain),
some on a mounted volume (cheap, big), some in S3/MinIO (durable, offsite) — and some want them
gone the moment OCR finishes. This module defines the narrow contract every backend implements
so the pipeline never learns which one is configured.

Two invariants hold across every backend:

* **Tenant namespacing is the store's job, not the key's.** Callers pass ``client_id`` on every
  operation; each backend physically isolates tenants (RLS predicate, per-client directory,
  per-client S3 prefix) rather than trusting the key to be well-formed.
* **Keys are never used as paths verbatim.** :func:`blob_key` produces a path-safe key, and the
  filesystem backend additionally hashes it — a hostile key cannot escape its tenant's tree.
"""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

#: Backend identifiers — also the accepted values of ``Settings.blob_backend``.
BACKEND_POSTGRES = "postgres"
BACKEND_LOCAL = "local"
BACKEND_S3 = "s3"
BACKEND_NONE = "none"

#: Anything outside this set is collapsed to "_" in a key segment. Note "/" is *not* in the set,
#: so no sanitized segment can introduce a path separator; ".." survives sanitization but is
#: stripped by :func:`_safe_segment` and is inert without a separator.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_SEGMENT = 80


class BlobStoreError(Exception):
    """A backend is misconfigured, unreachable, or was handed a request it must refuse."""


class BlobNotFound(Exception):
    """The requested blob does not exist (or is not visible to the calling tenant)."""


class BlobRef(BaseModel):
    """A pointer to stored bytes, returned by :meth:`BlobStore.put`.

    Attributes:
        uri: Backend-qualified locator — ``pg://{client_id}/{key}``, ``file://{abs_path}``,
            ``s3://{bucket}/{key}``, or ``""`` for the no-op backend.
        backend: One of ``postgres``, ``local``, ``s3``, ``none``.
        size: Length of the stored payload in bytes.
        content_type: MIME type as declared by the uploader, if known.
        sha256: Hex digest of the payload, computed at ``put`` time.
    """

    uri: str
    backend: str
    size: int
    content_type: str | None = None
    sha256: str | None = None


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _safe_segment(raw: str, fallback: str) -> str:
    """Collapse ``raw`` into a single path-safe segment.

    Separators and other unsafe characters become ``_``; leading/trailing dots, dashes and
    underscores are stripped so a segment can never be ``.`` or ``..``.

    Args:
        raw: Untrusted input.
        fallback: Segment to use when ``raw`` sanitizes to nothing.

    Returns:
        A non-empty segment containing only ``[A-Za-z0-9._-]``, at most 80 characters.
    """
    cleaned = _UNSAFE_RE.sub("_", raw.strip()).strip("._-")
    return cleaned[:_MAX_SEGMENT].strip("._-") or fallback


def _safe_filename(raw: str) -> str:
    """Reduce ``raw`` to its basename and sanitize it (both POSIX and Windows separators)."""
    basename = raw.replace("\\", "/").rsplit("/", 1)[-1]
    return _safe_segment(basename, "blob")


def blob_key(client_id: str, sha256: str, filename: str) -> str:
    """Derive the stable, path-safe storage key for a document's bytes.

    The key is a pure function of its inputs, so re-uploading identical bytes under the same
    name addresses the same blob (backends upsert). Content-addressing by ``sha256`` keeps two
    same-named-but-different documents apart.

    The key is deliberately **tenant-relative**: every backend applies its own tenant scoping
    (Postgres by ``client_id`` column, local by a hashed per-client directory, S3 by a key
    prefix it controls) rather than trusting a tenant segment supplied inside the key. Putting
    the client id here as well would only duplicate it into every path — e.g. an S3 key of
    ``documents/acme/acme/<sha>/passport.pdf``.

    ``client_id`` is retained in the signature because callers pass it and backends may need it
    for their own scoping.

    Args:
        client_id: Owning tenant (not embedded in the returned key; see above).
        sha256: Hex digest of the payload.
        filename: Original upload filename; only its basename is used.

    Returns:
        A key of the form ``{sha256}/{filename}``, containing no ``..`` segment and no separator
        other than the one shown.

    Example:
        >>> blob_key("acme", "ab" * 32, "../../etc/passwd")
        'abababababababababababababababababababababababababababababababab/passwd'
    """
    del client_id  # tenant scoping is the backend's responsibility, not the key's
    return "/".join(
        (
            _safe_segment(sha256.lower(), "_nodigest"),
            _safe_filename(filename),
        )
    )


class BlobStore(ABC):
    """Interface for raw-bytes storage. Implementations are tenant-scoped on every call."""

    backend: str

    @abstractmethod
    async def put(
        self, *, client_id: str, key: str, data: bytes, content_type: str | None = None
    ) -> BlobRef:
        """Store ``data`` for ``client_id`` under ``key``, replacing any existing blob.

        Args:
            client_id: Owning tenant.
            key: Storage key, normally from :func:`blob_key`. Treated as opaque and untrusted.
            data: Payload bytes.
            content_type: MIME type to record alongside the payload.

        Returns:
            A :class:`BlobRef` locating the stored bytes.

        Raises:
            BlobStoreError: The backend is unavailable or rejected the write.
        """

    @abstractmethod
    async def get(self, uri: str, *, client_id: str) -> bytes:
        """Fetch the bytes at ``uri`` on behalf of ``client_id``.

        Args:
            uri: A ``uri`` previously returned by :meth:`put`.
            client_id: Tenant making the request; a ``uri`` owned by another tenant is refused.

        Returns:
            The stored payload.

        Raises:
            BlobNotFound: No such blob.
            BlobStoreError: Malformed/foreign ``uri``, or the backend is unavailable.
        """

    @abstractmethod
    async def delete(self, uri: str, *, client_id: str) -> bool:
        """Delete the blob at ``uri``.

        Args:
            uri: A ``uri`` previously returned by :meth:`put`.
            client_id: Tenant making the request.

        Returns:
            True if a blob was deleted, False if it was already absent.

        Raises:
            BlobStoreError: Malformed/foreign ``uri``, or the backend is unavailable.
        """

    @abstractmethod
    async def delete_client(self, client_id: str) -> int:
        """Purge every blob belonging to ``client_id`` (tenant offboarding / right-to-erasure).

        Args:
            client_id: Tenant to purge.

        Returns:
            Number of blobs deleted.

        Raises:
            BlobStoreError: The backend is unavailable or the purge failed part-way.
        """

    @abstractmethod
    async def health(self) -> dict[str, Any]:
        """Probe the backend.

        Returns:
            ``{"backend": str, "ok": bool, "detail": str}``. Never raises.
        """


class NoneBlobStore(BlobStore):
    """No-op store for operators who do not want raw bytes retained at all.

    ``put`` accepts and discards the payload (reporting size/digest so the caller's audit trail
    stays intact); every read reports :class:`BlobNotFound`.
    """

    backend = BACKEND_NONE

    async def put(
        self, *, client_id: str, key: str, data: bytes, content_type: str | None = None
    ) -> BlobRef:
        """Discard ``data`` and return a ref with an empty ``uri``."""
        return BlobRef(
            uri="",
            backend=BACKEND_NONE,
            size=len(data),
            content_type=content_type,
            sha256=sha256_hex(data),
        )

    async def get(self, uri: str, *, client_id: str) -> bytes:
        """Always raise :class:`BlobNotFound` — nothing was retained."""
        raise BlobNotFound("blob retention is disabled (blob_backend=none)")

    async def delete(self, uri: str, *, client_id: str) -> bool:
        """Return False — there is never anything to delete."""
        return False

    async def delete_client(self, client_id: str) -> int:
        """Return 0 — there is never anything to purge."""
        return 0

    async def health(self) -> dict[str, Any]:
        """Report healthy; the no-op store has no dependencies."""
        return {"backend": BACKEND_NONE, "ok": True, "detail": "retention disabled"}
