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
import ssl
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
#: every tenant-scoped table that must carry a FORCE-RLS tenant_isolation policy (004 + 005).
_TENANT_TABLES = (
    "di_documents", "doc_version", "di_entity", "client_merged_fact", "di_decision_trace",
    "knode", "arep", "di_job", "di_blob", "di_fact_adjudication",
)
#: (table, extra-policy-name) exceptions to "tenant_isolation is the only policy" — reserved here,
#: before the durable-queue upgrade lands, so the posture guard never needs to change shape again.
_ALLOWED_EXTRA_POLICIES: dict[str, str] = {"di_job": "worker_claim"}


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


def _ssl_context(settings: Settings) -> ssl.SSLContext | None:
    """Build a verify-full TLS context when ``pg_ssl_root_cert`` is set; otherwise let asyncpg use
    its default (``sslmode`` effectively "prefer" against a server that offers TLS, unverified)."""
    if not settings.pg_ssl_root_cert:
        return None
    ctx = ssl.create_default_context(cafile=settings.pg_ssl_root_cert)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


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
        ssl=_ssl_context(settings),
        min_size=settings.pg_pool_min,
        max_size=settings.pg_pool_max,
        command_timeout=60,
        max_inactive_connection_lifetime=300,
        init=_init_conn,
    )
    return _pool


async def open_migration_connection(settings: Settings | None = None) -> asyncpg.Connection:
    """Open a single dedicated connection as the migration/owner role.

    ``pg_migration_user`` falls back to ``pg_user`` when unset, so a bare local run or a compose
    demo that has not been through the RLS role split (``tools/bootstrap_roles.sql`` /
    ``docker/initdb/01_roles.sql``) keeps working with a single role. In a production deployment
    with the role split applied, this is the ONLY connection in the process that ever runs DDL —
    the runtime pool (:func:`init_pool`) connects as the least-privilege runtime role and never
    sees these credentials.

    Callers own the connection's lifetime and must ``await conn.close()`` when done; this is a
    single short-lived connection, not a pool entry.
    """
    settings = settings or get_settings()
    conn = await asyncpg.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_migration_user or settings.pg_user,
        password=settings.pg_migration_password or settings.pg_password or None,
        database=settings.pg_database,
        ssl=_ssl_context(settings),
    )
    return conn


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


def _drift_message(filename: str, ledger_checksum: str, file_checksum: str) -> str:
    return (
        f"migration {filename} has already been applied with checksum {ledger_checksum[:12]}… "
        f"but the shipped file now checksums to {file_checksum[:12]}… — migration files must "
        f"never be edited after release (see di/migrations/005_hardening.sql header). Refusing "
        f"to boot: either revert the file or ship the change as a new NNN_*.sql."
    )


async def _assert_no_drift(conn: asyncpg.Connection, settings: Settings) -> None:
    """Refuse to boot if a previously-applied migration file was mutated.

    Without this, ``run_migrations`` would silently re-execute a changed file (the checksum
    mismatch is exactly the condition that used to trigger a re-run) — a mutated historical
    migration is a bug, not a new change, and must never replay against a live database.
    """
    applied = await _applied_migrations(conn, settings)
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        ledger_checksum = applied.get(path.name)
        if ledger_checksum is None:
            continue
        file_checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        if file_checksum != ledger_checksum:
            raise RuntimeError(_drift_message(path.name, ledger_checksum, file_checksum))


async def run_migrations(settings: Settings | None = None, *,
                         connection: asyncpg.Connection | None = None) -> None:
    """Apply every ``NNN_*.sql`` once, build hash partitions, and (with pgvector) add embedding
    columns + HNSW indexes.

    A session advisory lock serializes concurrent replicas so a rolling deploy cannot race DDL,
    a checksum ledger records what has actually been applied (files whose checksum already
    matches are skipped), and boot is refused outright if a previously-applied file's checksum no
    longer matches the ledger (drift guard) — mutating an old migration is a bug, never a
    legitimate change. Each file's DDL and its ledger row are written in one transaction, so a
    crash between them can never leave an applied-but-unledgered file that would silently replay.

    Args:
        settings: Settings to run against; defaults to :func:`get_settings`.
        connection: Run on this connection instead of a fresh runtime-pool checkout. Used by the
            RLS role split (phase 1) to run migrations on a dedicated owner connection rather than
            the runtime pool, which may hold a least-privilege role with no DDL rights.
    """
    settings = settings or get_settings()

    async def _run(conn: asyncpg.Connection) -> None:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{settings.pg_schema}";')
        await _init_conn(conn)
        lock_key = _lock_key(settings.pg_schema)
        await conn.execute("SELECT pg_advisory_lock($1)", lock_key)
        try:
            await _try_enable_pgvector(conn)
            await _ensure_ledger(conn, settings)
            await _assert_no_drift(conn, settings)
            applied = await _applied_migrations(conn, settings)

            for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                raw = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                if applied.get(path.name) == checksum:
                    logger.debug("migration %s already applied", path.name)
                    continue
                sql = raw.replace("__SCHEMA__", f'"{settings.pg_schema}"')
                async with conn.transaction():
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

    if connection is not None:
        await _run(connection)
        return
    pool = await init_pool(settings)
    async with pool.acquire() as conn:
        await _run(conn)


async def verify_migrations(settings: Settings | None = None) -> None:
    """Verify (without applying) that every shipped migration is in the ledger with a matching
    checksum. Used by ``MIGRATIONS_MODE=verify`` so a runtime instance never needs DDL rights.

    Raises:
        RuntimeError: naming every pending or drifted file.
    """
    settings = settings or get_settings()
    pool = await init_pool(settings)
    async with pool.acquire() as conn:
        applied = await _applied_migrations(conn, settings)
        problems: list[str] = []
        try:
            await _assert_partition_count(conn, settings)
        except RuntimeError as exc:
            problems.append(str(exc))
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        file_checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        ledger_checksum = applied.get(path.name)
        if ledger_checksum is None:
            problems.append(f"{path.name}: not yet applied")
        elif ledger_checksum != file_checksum:
            problems.append(f"{path.name}: checksum drift (ledger {ledger_checksum[:12]}… vs "
                            f"file {file_checksum[:12]}…)")
    if problems:
        raise RuntimeError(
            "migrations_mode=verify: schema does not match the shipped migration files — "
            "run `python -m di.migrate` before starting this instance: " + "; ".join(problems)
        )


# ---------------------------------------------------------------------------
# RLS posture — runtime-observed truth (pairs with di.posture's static checks)
# ---------------------------------------------------------------------------
def evaluate_rls_posture(*, rls_enabled: bool, rolsuper: bool, rolbypassrls: bool,
                         tenant_tables: tuple[str, ...],
                         policies_by_table: dict[str, list[str]],
                         tenant_isolation_predicate_ok: dict[str, bool],
                         partition_leaks: list[str]) -> list[str]:
    """Pure decision function: turn observed facts about the DB connection and its RLS policies
    into a list of violation strings. Empty list means the posture is sound.

    Kept free of any DB access so every branch (missing policy, wrong predicate, superuser
    connection, partition-privilege leak, an unexpected extra policy) is unit-testable with plain
    synthetic dicts — no live Postgres required.
    """
    violations: list[str] = []
    if not rls_enabled:
        violations.append("RLS_ENABLED is false")
    if rolsuper:
        violations.append("runtime role is a superuser — RLS (even FORCE) is bypassed entirely")
    if rolbypassrls:
        violations.append("runtime role has BYPASSRLS — RLS is bypassed entirely")
    for table in tenant_tables:
        names = policies_by_table.get(table, [])
        if "tenant_isolation" not in names:
            violations.append(f"{table}: missing the tenant_isolation policy")
        elif not tenant_isolation_predicate_ok.get(table, False):
            violations.append(
                f"{table}: tenant_isolation policy predicate does not reference "
                "app.current_client_id — it may not actually be filtering by tenant"
            )
        allowed_extra = _ALLOWED_EXTRA_POLICIES.get(table)
        extra = [n for n in names if n != "tenant_isolation" and n != allowed_extra]
        if extra:
            violations.append(f"{table}: unexpected extra polic{'y' if len(extra) == 1 else 'ies'}: "
                              f"{sorted(extra)}")
    if partition_leaks:
        violations.append(
            "runtime role has direct table privileges on hash partitions (bypasses the parent's "
            "RLS policy entirely): " + ", ".join(sorted(partition_leaks))
        )
    return violations


async def _sample_partition(conn: asyncpg.Connection, schema: str, table: str) -> str | None:
    """Return the name of one partition of ``table``, or ``None`` if it has none yet."""
    row = await conn.fetchrow(
        "SELECT c.relname FROM pg_inherits i "
        "JOIN pg_class c ON c.oid = i.inhrelid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "JOIN pg_namespace n ON n.oid = p.relnamespace "
        "WHERE n.nspname = $1 AND p.relname = $2 ORDER BY c.relname LIMIT 1",
        schema, table,
    )
    return row["relname"] if row else None


async def assert_rls_posture(settings: Settings | None = None) -> list[str]:
    """Gather the runtime-observed RLS facts (connected role, policies, partition privileges) via
    the runtime pool and evaluate them with :func:`evaluate_rls_posture`.

    Returns the list of violations (empty means sound) rather than raising directly — the caller
    decides whether to crash (production) or log a warning (local/dev), matching
    :func:`di.posture.assert_production_posture`'s fail-closed-only-in-production shape.
    """
    settings = settings or get_settings()
    pool = await init_pool(settings)
    async with pool.acquire() as conn:
        role_row = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        # An unresolvable current_user is itself suspicious; fail closed rather than assume safe.
        rolsuper = bool(role_row["rolsuper"]) if role_row else True
        rolbypassrls = bool(role_row["rolbypassrls"]) if role_row else True

        policy_rows = await conn.fetch(
            "SELECT tablename, policyname, qual FROM pg_policies WHERE schemaname = $1",
            settings.pg_schema,
        )
        policies_by_table: dict[str, list[str]] = {}
        predicate_ok: dict[str, bool] = {}
        for r in policy_rows:
            policies_by_table.setdefault(r["tablename"], []).append(r["policyname"])
            if r["policyname"] == "tenant_isolation":
                predicate_ok[r["tablename"]] = "current_client_id" in (r["qual"] or "")

        leaks: list[str] = []
        for table in _PARTITIONED_TABLES:
            part = await _sample_partition(conn, settings.pg_schema, table)
            if part is None:
                continue
            has_priv = await conn.fetchval(
                "SELECT has_table_privilege(current_user, format('%I.%I', $1::text, $2::text), "
                "'SELECT')",
                settings.pg_schema, part,
            )
            if has_priv:
                leaks.append(f"{settings.pg_schema}.{part}")

    return evaluate_rls_posture(
        rls_enabled=settings.rls_enabled, rolsuper=rolsuper, rolbypassrls=rolbypassrls,
        tenant_tables=_TENANT_TABLES, policies_by_table=policies_by_table,
        tenant_isolation_predicate_ok=predicate_ok, partition_leaks=leaks,
    )


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
