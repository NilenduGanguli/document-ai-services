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


async def run_migrations(settings: Settings | None = None) -> None:
    """Create the schema, apply every ``NNN_*.sql`` in order, build hash partitions, and
    (when pgvector is present) add embedding columns + HNSW indexes. Idempotent."""
    settings = settings or get_settings()
    pool = await init_pool(settings)
    async with pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{settings.pg_schema}";')
        await _init_conn(conn)
        await _discover_pgvector(conn)

        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text(encoding="utf-8").replace("__SCHEMA__", f'"{settings.pg_schema}"')
            try:
                await conn.execute(sql)
                logger.info("applied migration %s", path.name)
            except Exception:
                logger.exception("migration %s failed", path.name)
                raise

        for table in _PARTITIONED_TABLES:
            await _create_hash_partitions(conn, table, settings.pg_hash_partitions)

        await _ensure_vector_columns(conn, settings)


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
