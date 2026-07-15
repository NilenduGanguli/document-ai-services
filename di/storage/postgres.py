"""Postgres blob backend — payloads as ``bytea`` in ``di_blob``.

The default. Bytes live in the same database, backup domain and RLS perimeter as the knowledge
tree they came from, which is the simplest thing that is correct for small KYC documents (a few
MB of scans). Operators with large corpora should switch ``blob_backend`` to ``local`` or ``s3``
rather than growing the Postgres heap.

The ``di_blob`` table is created by migration 005 — this module never issues DDL. asyncpg maps
``bytea`` to/from Python ``bytes`` natively, and every statement runs through
``di.db.acquire(client_id)`` so the RLS tenant predicate is bound for the checkout.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg

from di.config import get_settings
from di.db import acquire
from di.storage.base import (
    BACKEND_POSTGRES,
    BlobNotFound,
    BlobRef,
    BlobStore,
    BlobStoreError,
    sha256_hex,
)

logger = logging.getLogger(__name__)

_URI_SCHEME = "pg://"


def _schema() -> str:
    """Return the configured schema name (never hardcoded)."""
    return get_settings().pg_schema


def _parse_pg_uri(uri: str) -> tuple[str, str]:
    """Split ``pg://{client_id}/{key}`` into its parts.

    Raises:
        BlobStoreError: The URI is not a well-formed ``pg://`` blob URI.
    """
    if not uri.startswith(_URI_SCHEME):
        raise BlobStoreError(f"not a postgres blob uri: {uri!r}")
    client_id, _, key = uri[len(_URI_SCHEME) :].partition("/")
    if not client_id or not key:
        raise BlobStoreError(f"malformed postgres blob uri: {uri!r}")
    return client_id, key


def _key_for(uri: str, client_id: str) -> str:
    """Extract the key from ``uri``, refusing a URI owned by a different tenant."""
    owner, key = _parse_pg_uri(uri)
    if owner != client_id:
        raise BlobStoreError(f"blob uri belongs to tenant {owner!r}, not {client_id!r}")
    return key


class PostgresBlobStore(BlobStore):
    """Store blobs as ``bytea`` rows in ``di_blob``, isolated by client_id + RLS."""

    backend = BACKEND_POSTGRES

    async def put(
        self, *, client_id: str, key: str, data: bytes, content_type: str | None = None
    ) -> BlobRef:
        """Upsert the payload on the ``(client_id, key)`` unique constraint."""
        schema = _schema()
        digest = sha256_hex(data)
        try:
            async with acquire(client_id) as conn:
                await conn.execute(
                    f'INSERT INTO "{schema}".di_blob '
                    "(id, client_id, key, content_type, size, sha256, data) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7) "
                    "ON CONFLICT (client_id, key) DO UPDATE SET "
                    "content_type = EXCLUDED.content_type, size = EXCLUDED.size, "
                    "sha256 = EXCLUDED.sha256, data = EXCLUDED.data",
                    str(uuid.uuid4()), client_id, key, content_type, len(data), digest, data,
                )
        except asyncpg.PostgresError as exc:
            raise BlobStoreError(f"postgres blob write failed: {exc}") from exc
        return BlobRef(
            uri=f"{_URI_SCHEME}{client_id}/{key}",
            backend=self.backend,
            size=len(data),
            content_type=content_type,
            sha256=digest,
        )

    async def get(self, uri: str, *, client_id: str) -> bytes:
        """Fetch the payload for ``uri``."""
        key = _key_for(uri, client_id)
        schema = _schema()
        try:
            async with acquire(client_id) as conn:
                row = await conn.fetchrow(
                    f'SELECT data FROM "{schema}".di_blob WHERE client_id = $1 AND key = $2',
                    client_id, key,
                )
        except asyncpg.PostgresError as exc:
            raise BlobStoreError(f"postgres blob read failed: {exc}") from exc
        if row is None:
            raise BlobNotFound(f"no blob at {uri}")
        return bytes(row["data"])

    async def delete(self, uri: str, *, client_id: str) -> bool:
        """Delete the row for ``uri``; returns whether a row existed."""
        key = _key_for(uri, client_id)
        schema = _schema()
        try:
            async with acquire(client_id) as conn:
                row = await conn.fetchrow(
                    f'DELETE FROM "{schema}".di_blob WHERE client_id = $1 AND key = $2 '
                    "RETURNING id",
                    client_id, key,
                )
        except asyncpg.PostgresError as exc:
            raise BlobStoreError(f"postgres blob delete failed: {exc}") from exc
        return row is not None

    async def delete_client(self, client_id: str) -> int:
        """Delete every blob row for the tenant."""
        schema = _schema()
        try:
            async with acquire(client_id) as conn:
                status = await conn.execute(
                    f'DELETE FROM "{schema}".di_blob WHERE client_id = $1', client_id
                )
        except asyncpg.PostgresError as exc:
            raise BlobStoreError(f"postgres blob purge failed: {exc}") from exc
        # asyncpg returns the command tag, e.g. "DELETE 12".
        try:
            return int(status.split()[-1])
        except (AttributeError, IndexError, ValueError):  # pragma: no cover - defensive
            return 0

    async def health(self) -> dict[str, Any]:
        """Check the pool is reachable and ``di_blob`` exists. Global (non-tenant) query."""
        schema = _schema()
        try:
            async with acquire(None) as conn:
                found = await conn.fetchval("SELECT to_regclass($1)", f'"{schema}".di_blob')
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return {"backend": self.backend, "ok": False, "detail": str(exc)}
        if found is None:
            return {
                "backend": self.backend,
                "ok": False,
                "detail": f'"{schema}".di_blob is missing (run migrations)',
            }
        return {"backend": self.backend, "ok": True, "detail": f'table "{schema}".di_blob'}
