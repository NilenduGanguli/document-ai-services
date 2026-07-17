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
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from di import ratelimit
from di.config import get_settings
from di.db import acquire

logger = logging.getLogger(__name__)

#: Header carrying the raw API key.
API_KEY_HEADER = "X-API-KEY"
#: Prefix on every generated key — makes keys greppable in secret scanners.
KEY_PREFIX = "di_"
#: Grant value meaning "all tenants" (in ``client_ids``) or "all scopes" (in ``scopes``).
WILDCARD = "*"
#: How long a resolved principal stays memoized (upper bound — an expiring key uses a shorter TTL
#: so it stops authenticating on time despite the memo; see :func:`resolve_principal`).
CACHE_TTL_SECONDS = 30.0
#: Ceiling on the rotation overlap window a caller may request.
MAX_ROTATION_OVERLAP_HOURS = 168  # 1 week

_LOCAL_DEV_KEY_ID = "local-dev"
_BOOTSTRAP_NAME = "bootstrap"
_WWW_AUTHENTICATE = f'ApiKey realm="document-ai-services", header="{API_KEY_HEADER}"'

# Columns safe to expose — deliberately excludes key_hash.
_PUBLIC_COLS = (
    "id, name, client_ids, scopes, created_at, last_used_at, disabled_at, "
    "expires_at, rotated_from, rate_limit_rps, created_by"
)


class Principal(BaseModel):
    """An authenticated API caller and the grants attached to its key."""

    key_id: str
    name: str
    client_ids: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    rate_limit_rps: float | None = None

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
        rate_limit_rps=row["rate_limit_rps"],
    )


def _unauthorized(detail: str) -> HTTPException:
    """Build a 401 that advertises the expected scheme but never echoes the supplied key."""
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": _WWW_AUTHENTICATE},
    )


async def create_api_key(*, name: str, client_ids: list[str], scopes: list[str],
                         expires_at: datetime | None = None, created_by: str | None = None,
                         rotated_from: str | None = None,
                         rate_limit_rps: float | None = None) -> tuple[str, str]:
    """Create an API key and return its id plus the raw secret.

    The raw key is returned exactly once and is never persisted — only its hash is stored. Callers
    must surface it to the operator immediately; it is unrecoverable afterwards.

    Args:
        name: Human label for the key, e.g. ``"acme-ingest-worker"``.
        client_ids: Tenants the key may access; ``["*"]`` grants every tenant.
        scopes: Scopes the key holds; ``["*"]`` grants every scope.
        expires_at: When the key stops authenticating. ``None`` means it never expires — prefer
            setting this for anything beyond a local demo.
        created_by: Best-effort attribution (the calling principal's name, or a CLI operator tag).
        rotated_from: The predecessor key's id, when this key was minted by :func:`rotate_api_key`.
        rate_limit_rps: Per-key override for the rate-limit backstop; ``None`` uses the fleet
            default (``settings.rate_limit_default_rps``).

    Returns:
        Tuple of ``(key_id, raw_key)``.
    """
    s = _schema()
    raw_key = generate_key()
    key_id = str(uuid.uuid4())
    async with acquire(None) as conn:
        await conn.execute(
            f'INSERT INTO "{s}".di_api_key '
            "(id, key_hash, name, client_ids, scopes, expires_at, created_by, rotated_from, "
            " rate_limit_rps) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            key_id, hash_key(raw_key), name, list(client_ids), list(scopes), expires_at,
            created_by, uuid.UUID(rotated_from) if rotated_from else None, rate_limit_rps,
        )
    logger.info("created api key id=%s name=%s expires_at=%s", key_id, name, expires_at)
    return key_id, raw_key


async def rotate_api_key(key_id: str, *, overlap_hours: int | None = None,
                         ) -> tuple[str, str, datetime]:
    """Rotate a key: mint a successor with identical grants, and time-box the predecessor.

    Flow: create-new -> overlap-window -> old key auto-expires. ``revoke_api_key`` remains the
    immediate kill switch if the old key must die sooner than the overlap window.

    Args:
        key_id: The key to rotate.
        overlap_hours: How long the old key stays valid after rotation; defaults to
            ``settings.key_rotation_overlap_hours``, clamped to
            :data:`MAX_ROTATION_OVERLAP_HOURS`.

    Returns:
        ``(new_key_id, new_raw_key, old_key_expires_at)``.

    Raises:
        HTTPException: 404 if the key is unknown, disabled, or already expired.
    """
    settings = get_settings()
    hours = overlap_hours if overlap_hours is not None else settings.key_rotation_overlap_hours
    hours = max(1, min(hours, MAX_ROTATION_OVERLAP_HOURS))
    s = _schema()
    async with acquire(None) as conn, conn.transaction():
        # FOR UPDATE fences concurrent rotations of the same key: the second caller blocks until
        # the first commits, then sees disabled_at/expires_at already moved and 404s cleanly
        # instead of minting a second successor (the partial unique index on rotated_from in
        # 007_auth_hardening.sql is the defense-in-depth backstop for this same race).
        old = await conn.fetchrow(
            f'SELECT id, name, client_ids, scopes, rate_limit_rps, expires_at, disabled_at '
            f'FROM "{s}".di_api_key WHERE id = $1 FOR UPDATE',
            key_id,
        )
        if old is None or old["disabled_at"] is not None:
            raise HTTPException(status_code=404, detail="key not found or already disabled")
        if old["expires_at"] is not None and old["expires_at"] <= datetime.now(old["expires_at"].tzinfo):
            raise HTTPException(status_code=404, detail="key has already expired")

        overlap_expiry = datetime.now(tz=old["expires_at"].tzinfo if old["expires_at"] else None) \
            + timedelta(hours=hours)
        new_expires_at = min(old["expires_at"], overlap_expiry) if old["expires_at"] else overlap_expiry

        raw_key = generate_key()
        new_key_id = str(uuid.uuid4())
        rotated_name = f"{old['name']}@{datetime.now().strftime('%Y%m%d')}"
        await conn.execute(
            f'INSERT INTO "{s}".di_api_key '
            "(id, key_hash, name, client_ids, scopes, rotated_from, rate_limit_rps) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            new_key_id, hash_key(raw_key), rotated_name, list(old["client_ids"] or []),
            list(old["scopes"] or []), uuid.UUID(key_id), old["rate_limit_rps"],
        )
        await conn.execute(
            f'UPDATE "{s}".di_api_key SET expires_at = $1 WHERE id = $2',
            new_expires_at, key_id,
        )
    for cached_hash, (_, principal) in list(_cache.items()):
        if principal.key_id == key_id:
            _cache.pop(cached_hash, None)
    logger.info("rotated api key id=%s -> new_key_id=%s overlap_hours=%d", key_id, new_key_id, hours)
    return new_key_id, raw_key, new_expires_at


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
    directly. Successful resolutions are memoized for up to :data:`CACHE_TTL_SECONDS` — capped
    tighter when the key has an ``expires_at``, so it stops authenticating on time despite the
    memo — and ``last_used_at`` is refreshed (best-effort) on each cache miss. Unknown/expired/
    disabled lookups are recorded in the short-lived failed-auth backstop (``di.ratelimit``) so a
    credential-stuffing flood cannot turn into one uncacheable Postgres lookup per request.

    Args:
        raw_key: The raw key from the ``X-API-KEY`` header.

    Returns:
        The :class:`Principal`, or None if the key is empty, unknown, expired, or disabled.
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

    if ratelimit.check_failed_auth_backstop(key_hash):
        return None

    s = _schema()
    async with acquire(None) as conn:
        row = await conn.fetchrow(
            f'SELECT {_PUBLIC_COLS} FROM "{s}".di_api_key '
            "WHERE key_hash = $1 AND disabled_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now())",
            key_hash,
        )
    if row is None:
        ratelimit.record_auth_failure(key_hash)
        return None

    principal = _row_to_principal(row)
    ttl = CACHE_TTL_SECONDS
    expires_at = row["expires_at"]
    if expires_at is not None:
        seconds_left = (expires_at - datetime.now(tz=expires_at.tzinfo)).total_seconds()
        ttl = max(0.0, min(ttl, seconds_left))
    _cache[key_hash] = (time.monotonic() + ttl, principal)
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

    Also enforces the per-key rate-limit backstop (``settings.rate_limit_enabled``) and stashes
    the resolved principal on ``request.state.principal`` for the access-audit middleware.

    Args:
        request: The incoming request.

    Returns:
        The authenticated :class:`Principal`. When ``settings.auth_enabled`` is False, a wildcard
        local-dev principal is returned without a DB round-trip or rate limiting.

    Raises:
        HTTPException: 401 when the header is missing, or the key is unknown, expired, or
            disabled (the supplied key is never echoed back); 429 when the key's rate-limit
            backstop is exhausted.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        principal = _local_dev_principal()
        request.state.principal = principal
        return principal
    raw_key = request.headers.get(API_KEY_HEADER)
    if not raw_key:
        raise _unauthorized(f"missing API key: supply the {API_KEY_HEADER} header")
    principal = await resolve_principal(raw_key)
    if principal is None:
        raise _unauthorized("invalid or disabled API key")
    request.state.principal = principal

    if settings.rate_limit_enabled and principal.key_id != _LOCAL_DEV_KEY_ID:
        rps = principal.rate_limit_rps or settings.rate_limit_default_rps
        allowed, retry_after = ratelimit.check_rate_limit(principal.key_id, rps=rps)
        if not allowed:
            raise HTTPException(
                status_code=429, detail="rate limit exceeded for this API key",
                headers={"Retry-After": str(max(1, int(retry_after) + 1))},
            )
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
