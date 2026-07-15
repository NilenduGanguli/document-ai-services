"""Async Postgres data layer (asyncpg + pgvector + ltree).

Responsibilities:
- a single shared connection pool with a per-connection ``search_path`` + jsonb codec;
- per-acquire Row-Level-Security GUC binding (``app.current_client_id``) for tenant isolation;
- idempotent, startup-applied migrations (``di/migrations/NNN_*.sql``) with a ``__SCHEMA__``
  token rewritten to the configured schema, plus programmatic HASH-partition creation;
- runtime addition of pgvector embedding columns + HNSW indexes (skipped cleanly when the
  ``vector`` extension is not installed — e.g. local dev without pgvector).

Mirrors the conventions of the sibling ``retrieval`` backend without depending on it.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

from di.config import Settings, get_settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pgvector_schema: str | None = None
_pgvector_checked = False
_embedding_dim: int | None = None

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
#: tables created as HASH-partitioned parents (partition key leads with client_id)
_PARTITIONED_TABLES = ("knode", "arep")


def vec_to_pg(vec: list[float]) -> str:
    """Format a Python float list into a pgvector text literal."""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


async def _init_conn(conn: asyncpg.Connection) -> None:
    settings = get_settings()
    parts = [f'"{settings.pg_schema}"']
    if _pgvector_schema and _pgvector_schema not in (settings.pg_schema, "public"):
        parts.append(f'"{_pgvector_schema}"')
    parts.append("public")
    await conn.execute(f"SET search_path TO {', '.join(parts)};")
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def init_pool(settings: Settings | None = None) -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    settings = settings or get_settings()
    _pool = await asyncpg.create_pool(
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_user,
        password=settings.pg_password or None,
        database=settings.pg_database,
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
        command_timeout=60,
        max_inactive_connection_lifetime=300,
        init=_init_conn,
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire(client_id: str | None = None) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a pooled connection. If ``client_id`` is given and RLS is enabled, bind the
    tenant GUC for the life of the checkout and reset it on release."""
    settings = get_settings()
    pool = await init_pool(settings)
    async with pool.acquire() as conn:
        await _init_conn(conn)  # defensive: pre-warmed conns may predate discovery
        bound = False
        if settings.rls_enabled and client_id is not None:
            await conn.execute("SELECT set_config('app.current_client_id', $1, false)", client_id)
            bound = True
        try:
            yield conn
        finally:
            if bound:
                await conn.execute("SELECT set_config('app.current_client_id', '', false)")


async def _discover_pgvector(conn: asyncpg.Connection) -> str | None:
    global _pgvector_schema, _pgvector_checked
    if _pgvector_checked:
        return _pgvector_schema
    row = await conn.fetchrow(
        "SELECT n.nspname FROM pg_extension e "
        "JOIN pg_namespace n ON e.extnamespace = n.oid WHERE e.extname = 'vector'"
    )
    _pgvector_schema = row[0] if row else None
    _pgvector_checked = True
    if _pgvector_schema is None:
        logger.warning("pgvector not installed; embedding columns/HNSW indexes will be skipped.")
    return _pgvector_schema


async def pgvector_available() -> bool:
    pool = await init_pool()
    async with pool.acquire() as conn:
        return await _discover_pgvector(conn) is not None


def set_embedding_dim(dim: int) -> None:
    """Lock the embedding dimension (typically from the retrieval service's /api/models)."""
    global _embedding_dim
    _embedding_dim = dim


def embedding_dim() -> int:
    return _embedding_dim or get_settings().embedding_dim_default


async def _create_hash_partitions(conn: asyncpg.Connection, table: str, n: int) -> None:
    schema = get_settings().pg_schema
    for i in range(n):
        await conn.execute(
            f'CREATE TABLE IF NOT EXISTS "{schema}"."{table}_p{i}" '
            f'PARTITION OF "{schema}"."{table}" '
            f"FOR VALUES WITH (MODULUS {n}, REMAINDER {i});"
        )


def _lock_key(schema: str) -> int:
    """Stable signed int64 advisory-lock key derived from the schema name."""
    return int.from_bytes(hashlib.sha256(schema.encode()).digest()[:8], "big", signed=True)


async def _ensure_ledger(conn: asyncpg.Connection, settings: Settings) -> None:
    """Bootstrap the migration ledger itself (it cannot be created by a ledgered migration)."""
    await conn.execute(
        f'CREATE TABLE IF NOT EXISTS "{settings.pg_schema}".di_migration_ledger ('
        " filename text PRIMARY KEY,"
        " checksum text NOT NULL,"
        " applied_at timestamptz NOT NULL DEFAULT now());"
    )


async def _applied_migrations(conn: asyncpg.Connection, settings: Settings) -> dict[str, str]:
    rows = await conn.fetch(
        f'SELECT filename, checksum FROM "{settings.pg_schema}".di_migration_ledger'
    )
    return {r["filename"]: r["checksum"] for r in rows}


async def _assert_partition_count(conn: asyncpg.Connection, settings: Settings) -> None:
    """A HASH partition count is frozen at first deploy: changing it silently corrupts routing.

    Fail loudly on mismatch rather than letting ``CREATE TABLE ... FOR VALUES WITH (MODULUS n)``
    produce overlap errors (increase) or leave orphaned rows unreachable (decrease).
    """
    for table in _PARTITIONED_TABLES:
        live = await conn.fetchval(
            "SELECT count(*) FROM pg_inherits i "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "JOIN pg_namespace n ON n.oid = p.relnamespace "
            "WHERE n.nspname = $1 AND p.relname = $2",
            settings.pg_schema, table,
        )
        if live and int(live) != settings.pg_hash_partitions:
            raise RuntimeError(
                f'{table} already has {live} hash partitions but PG_HASH_PARTITIONS='
                f"{settings.pg_hash_partitions}. The partition count is immutable after the first "
                f"deploy — set PG_HASH_PARTITIONS={live} or migrate the data deliberately."
            )


async def run_migrations(settings: Settings | None = None) -> None:
    """Apply every ``NNN_*.sql`` once, build hash partitions, and (with pgvector) add embedding
    columns + HNSW indexes.

    A session advisory lock serializes concurrent replicas so a rolling deploy cannot race DDL,
    and a checksum ledger records what has actually been applied (files whose checksum already
    matches are skipped, making non-idempotent migrations possible in future).
    """
    settings = settings or get_settings()
    pool = await init_pool(settings)
    async with pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{settings.pg_schema}";')
        await _init_conn(conn)
        lock_key = _lock_key(settings.pg_schema)
        await conn.execute("SELECT pg_advisory_lock($1)", lock_key)
        try:
            await _try_enable_pgvector(conn)
            await _ensure_ledger(conn, settings)
            applied = await _applied_migrations(conn, settings)

            for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                raw = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                if applied.get(path.name) == checksum:
                    logger.debug("migration %s already applied", path.name)
                    continue
                sql = raw.replace("__SCHEMA__", f'"{settings.pg_schema}"')
                try:
                    await conn.execute(sql)
                except Exception:
                    logger.exception("migration %s failed", path.name)
                    raise
                await conn.execute(
                    f'INSERT INTO "{settings.pg_schema}".di_migration_ledger '
                    "(filename, checksum) VALUES ($1,$2) "
                    "ON CONFLICT (filename) DO UPDATE SET checksum = EXCLUDED.checksum, "
                    "applied_at = now()",
                    path.name, checksum,
                )
                logger.info("applied migration %s", path.name)

            await _assert_partition_count(conn, settings)
            for table in _PARTITIONED_TABLES:
                await _create_hash_partitions(conn, table, settings.pg_hash_partitions)

            await _ensure_vector_columns(conn, settings)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", lock_key)


async def _try_enable_pgvector(conn: asyncpg.Connection) -> None:
    """Attempt ``CREATE EXTENSION vector`` (guarded), then (re)discover its schema.

    On the pgvector image the extension is *available* but not yet created; on plain Postgres
    without pgvector the CREATE fails and we degrade cleanly to FTS-only search.
    """
    global _pgvector_schema, _pgvector_checked
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    except Exception:  # noqa: BLE001 - extension unavailable on this server; degrade gracefully
        logger.info("pgvector extension unavailable; vector features disabled")
    _pgvector_checked = False
    _pgvector_schema = None
    await _discover_pgvector(conn)


async def _ensure_vector_columns(conn: asyncpg.Connection, settings: Settings) -> None:
    """Add ``embedding`` columns + HNSW indexes once the live dim is known. No-op without pgvector."""
    if await _discover_pgvector(conn) is None:
        return
    dim = embedding_dim()
    schema = settings.pg_schema
    targets = [("knode", "content_embedding"), ("arep", "rep_embedding")]
    for table, col in targets:
        exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = $2 AND column_name = $3",
            schema, table, col,
        )
        if not exists:
            await conn.execute(
                f'ALTER TABLE "{schema}"."{table}" ADD COLUMN {col} vector({dim});'
            )
        # HNSW per partition (pgvector indexes on partitioned parents are not supported directly)
        for i in range(settings.pg_hash_partitions):
            await conn.execute(
                f'CREATE INDEX IF NOT EXISTS "{table}_p{i}_{col}_hnsw" '
                f'ON "{schema}"."{table}_p{i}" USING hnsw ({col} vector_cosine_ops);'
            )
