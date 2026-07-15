"""API-key authentication + per-tenant authorization.

Keys are presented in the ``X-API-KEY`` header. Only the SHA-256 hash of a key is ever stored or
compared — the raw key exists exactly once, in the response of :func:`create_api_key`, and is
never written to the database or the logs.

A principal carries two independent grants:

* ``client_ids`` — which tenants it may touch (``["*"]`` = every tenant);
* ``scopes``     — what it may do (``["*"]`` = everything; known: ``ingest``, ``read``, ``admin``).

Authentication is skipped entirely when ``settings.auth_enabled`` is False (the offline local-dev
demo), in which case a wildcard principal is synthesized without touching the database.

Resolution is memoized for :data:`CACHE_TTL_SECONDS` keyed by key hash, so a hot path costs one
DB round-trip per key per TTL window rather than one per request. Call :func:`reset_auth_cache`
in tests (and after mutating keys out-of-band) to drop the memo.

The backing table ``di_api_key`` is global (not tenant-scoped) and is created by migration 005;
every statement here therefore uses ``acquire(None)``.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from di.config import get_settings
from di.db import acquire

logger = logging.getLogger(__name__)

#: Header carrying the raw API key.
API_KEY_HEADER = "X-API-KEY"
#: Prefix on every generated key — makes keys greppable in secret scanners.
KEY_PREFIX = "di_"
#: Grant value meaning "all tenants" (in ``client_ids``) or "all scopes" (in ``scopes``).
WILDCARD = "*"
#: How long a resolved principal stays memoized.
CACHE_TTL_SECONDS = 30.0

_LOCAL_DEV_KEY_ID = "local-dev"
_BOOTSTRAP_NAME = "bootstrap"
_WWW_AUTHENTICATE = f'ApiKey realm="document-intelligence", header="{API_KEY_HEADER}"'

# Columns safe to expose — deliberately excludes key_hash.
_PUBLIC_COLS = "id, name, client_ids, scopes, created_at, last_used_at, disabled_at"


class Principal(BaseModel):
    """An authenticated API caller and the grants attached to its key."""

    key_id: str
    name: str
    client_ids: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)

    def can_access(self, client_id: str) -> bool:
        """Return True if this principal may act on ``client_id``'s data.

        Args:
            client_id: Tenant identifier being requested.

        Returns:
            True when the key holds the wildcard tenant grant or lists ``client_id`` explicitly.
        """
        return WILDCARD in self.client_ids or client_id in self.client_ids

    def has_scope(self, scope: str) -> bool:
        """Return True if this principal holds ``scope``.

        Args:
            scope: Scope name, e.g. ``"ingest"``, ``"read"``, ``"admin"``.

        Returns:
            True when the key holds the wildcard scope grant or lists ``scope`` explicitly.
        """
        return WILDCARD in self.scopes or scope in self.scopes


# key_hash -> (monotonic_expiry, principal). Only successful resolutions are cached, so unknown
# keys cannot grow this dict without bound.
_cache: dict[str, tuple[float, Principal]] = {}


def _schema() -> str:
    return get_settings().pg_schema


def hash_key(raw: str) -> str:
    """Hash a raw API key for storage/lookup.

    Args:
        raw: The raw key as presented by the caller.

    Returns:
        Lowercase hex SHA-256 digest of ``raw``.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> str:
    """Mint a new random API key.

    Returns:
        A URL-safe key of the form ``di_<43-char token>``.
    """
    return f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"


def reset_auth_cache() -> None:
    """Drop every memoized principal. Call from tests and after out-of-band key changes."""
    _cache.clear()


def _local_dev_principal() -> Principal:
    """Build the wildcard principal used when auth is disabled (fresh instance each call)."""
    return Principal(
        key_id=_LOCAL_DEV_KEY_ID,
        name=_LOCAL_DEV_KEY_ID,
        client_ids=[WILDCARD],
        scopes=[WILDCARD],
    )


def _row_to_principal(row: Any) -> Principal:
    return Principal(
        key_id=str(row["id"]),
        name=row["name"],
        client_ids=list(row["client_ids"] or []),
        scopes=list(row["scopes"] or []),
    )


def _unauthorized(detail: str) -> HTTPException:
    """Build a 401 that advertises the expected scheme but never echoes the supplied key."""
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
    )


async def create_api_key(*, name: str, client_ids: list[str], scopes: list[str]) -> tuple[str, str]:
    """Create an API key and return its id plus the raw secret.

    The raw key is returned exactly once and is never persisted — only its hash is stored. Callers
    must surface it to the operator immediately; it is unrecoverable afterwards.

    Args:
        name: Human label for the key, e.g. ``"acme-ingest-worker"``.
        client_ids: Tenants the key may access; ``["*"]`` grants every tenant.
        scopes: Scopes the key holds; ``["*"]`` grants every scope.

    Returns:
        Tuple of ``(key_id, raw_key)``.
    """
    s = _schema()
    raw_key = generate_key()
    key_id = str(uuid.uuid4())
    async with acquire(None) as conn:
        await conn.execute(
            f'INSERT INTO "{s}".di_api_key (id, key_hash, name, client_ids, scopes) '
            "VALUES ($1,$2,$3,$4,$5)",
            key_id, hash_key(raw_key), name, list(client_ids), list(scopes),
        )
    logger.info("created api key id=%s name=%s", key_id, name)
    return key_id, raw_key


async def list_api_keys() -> list[dict[str, Any]]:
    """List every API key, newest first.

    Returns:
        One dict per key with id, name, client_ids, scopes and the created/last-used/disabled
        timestamps. Never includes the key hash or any raw key material.
    """
    s = _schema()
    async with acquire(None) as conn:
        rows = await conn.fetch(
            f'SELECT {_PUBLIC_COLS} FROM "{s}".di_api_key ORDER BY created_at DESC'
        )
    return [{**dict(r), "id": str(r["id"])} for r in rows]


async def revoke_api_key(key_id: str) -> bool:
    """Disable a key so it can no longer authenticate.

    Also evicts the key from the resolution cache so revocation takes effect immediately rather
    than after the TTL lapses.

    Args:
        key_id: The key's uuid.

    Returns:
        True if a live key was disabled; False if the id is unknown, malformed, or already
        disabled.
    """
    try:
        uuid.UUID(key_id)
    except ValueError:
        return False
    s = _schema()
    async with acquire(None) as conn:
        row = await conn.fetchrow(
            f'UPDATE "{s}".di_api_key SET disabled_at = now() '
            "WHERE id = $1 AND disabled_at IS NULL RETURNING id",
            key_id,
        )
    if row is None:
        return False
    for cached_hash, (_, principal) in list(_cache.items()):
        if principal.key_id == key_id:
            _cache.pop(cached_hash, None)
    logger.info("revoked api key id=%s", key_id)
    return True


async def _touch_last_used(key_hash: str) -> None:
    """Best-effort ``last_used_at`` bump; never fails the request it annotates."""
    s = _schema()
    try:
        async with acquire(None) as conn:
            await conn.execute(
                f'UPDATE "{s}".di_api_key SET last_used_at = now() WHERE key_hash = $1',
                key_hash,
            )
    except Exception:  # noqa: BLE001 - telemetry only; auth already succeeded
        logger.debug("could not update last_used_at", exc_info=True)


async def resolve_principal(raw_key: str) -> Principal | None:
    """Resolve a raw API key to its principal.

    Looks the key's hash up in ``di_api_key``; the raw key is never stored, logged, or compared
    directly. Successful resolutions are memoized for :data:`CACHE_TTL_SECONDS`, and
    ``last_used_at`` is refreshed (best-effort) on each cache miss.

    Args:
        raw_key: The raw key from the ``X-API-KEY`` header.

    Returns:
        The :class:`Principal`, or None if the key is empty, unknown, or disabled.
    """
    if not raw_key:
        return None
    key_hash = hash_key(raw_key)
    cached = _cache.get(key_hash)
    if cached is not None:
        expiry, principal = cached
        if time.monotonic() < expiry:
            return principal
        _cache.pop(key_hash, None)

    s = _schema()
    async with acquire(None) as conn:
        row = await conn.fetchrow(
            f'SELECT {_PUBLIC_COLS} FROM "{s}".di_api_key '
            "WHERE key_hash = $1 AND disabled_at IS NULL",
            key_hash,
        )
    if row is None:
        return None

    principal = _row_to_principal(row)
    _cache[key_hash] = (time.monotonic() + CACHE_TTL_SECONDS, principal)
    await _touch_last_used(key_hash)
    return principal


async def ensure_bootstrap_key() -> str | None:
    """Seed the wildcard bootstrap key from ``settings.di_bootstrap_api_key``.

    Lets a container come up with a working key straight from the environment. Idempotent: the
    insert is ``ON CONFLICT (key_hash) DO NOTHING``, and the existing row's id is returned on a
    repeat call. A bootstrap key that was explicitly revoked stays revoked — this will return its
    id but the key will not authenticate.

    Returns:
        The bootstrap key's id, or None when ``di_bootstrap_api_key`` is unset.
    """
    settings = get_settings()
    raw_key = settings.di_bootstrap_api_key
    if not raw_key:
        return None
    s = _schema()
    key_hash = hash_key(raw_key)
    async with acquire(None) as conn:
        row = await conn.fetchrow(
            f'INSERT INTO "{s}".di_api_key (id, key_hash, name, client_ids, scopes) '
            "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (key_hash) DO NOTHING RETURNING id",
            str(uuid.uuid4()), key_hash, _BOOTSTRAP_NAME, [WILDCARD], [WILDCARD],
        )
        if row is None:  # already seeded
            row = await conn.fetchrow(
                f'SELECT id FROM "{s}".di_api_key WHERE key_hash = $1', key_hash
            )
            if row is None:  # pragma: no cover - only if deleted between the two statements
                return None
            return str(row["id"])
    key_id = str(row["id"])
    logger.info("seeded bootstrap api key id=%s", key_id)
    return key_id


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
async def require_principal(request: Request) -> Principal:
    """FastAPI dependency: authenticate the caller from the ``X-API-KEY`` header.

    Args:
        request: The incoming request.

    Returns:
        The authenticated :class:`Principal`. When ``settings.auth_enabled`` is False, a wildcard
        local-dev principal is returned without a DB round-trip.

    Raises:
        HTTPException: 401 when the header is missing, or the key is unknown or disabled. The
            supplied key is never echoed back.
    """
    if not get_settings().auth_enabled:
        return _local_dev_principal()
    raw_key = request.headers.get(API_KEY_HEADER)
    if not raw_key:
        raise _unauthorized(f"missing API key: supply the {API_KEY_HEADER} header")
    principal = await resolve_principal(raw_key)
    if principal is None:
        raise _unauthorized("invalid or disabled API key")
    return principal


def require_scope(scope: str) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency that authenticates the caller and asserts a scope.

    Args:
        scope: Required scope, e.g. ``"ingest"``, ``"read"``, ``"admin"``.

    Returns:
        A FastAPI dependency yielding the :class:`Principal`, raising 403 if it lacks ``scope``.
    """

    async def _require_scope(
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> Principal:
        if not principal.has_scope(scope):
            raise HTTPException(status_code=403, detail=f"missing required scope: {scope}")
        return principal

    return _require_scope


def authorize_client(principal: Principal, client_id: str) -> None:
    """Assert that ``principal`` may act on ``client_id``.

    Args:
        principal: The authenticated caller.
        client_id: The tenant being accessed.

    Raises:
        HTTPException: 403 when the principal has no grant for ``client_id``.
    """
    if not principal.can_access(client_id):
        raise HTTPException(status_code=403, detail=f"not authorized for client: {client_id}")
