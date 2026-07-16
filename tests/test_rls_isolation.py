"""Integration proof that RLS actually isolates tenants under the least-privilege runtime role.

This is the auditor-facing artifact for the RLS production-posture upgrade: it proves — against a
live Postgres with the role split applied — that a compromised or buggy request handler running
as ``di_app`` cannot read or write another tenant's rows, cannot read another tenant's rows when no
tenant is bound at all, cannot bypass a table's RLS policy by querying its hash partition directly,
and does not hold the privileges (superuser, BYPASSRLS, DELETE on di_api_key, INSERT on the
migration ledger) that would undermine any of the above.

Requires the docker-compose stack (or an equivalent database) provisioned via
``docker/initdb/01_roles.sql`` or ``tools/bootstrap_roles.sql`` — i.e. the roles ``di_owner`` and
``di_app`` must exist. Skips the whole module cleanly if they don't, rather than failing: this
suite intentionally does NOT run under the default ``DI_RUN_INTEGRATION=1`` local-Postgres path
used by the rest of the DB-backed tests (those run against a single unsplit role), since RLS
enforcement is meaningless without the split roles in place.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from di.config import get_settings
from di.db import _TENANT_TABLES, run_migrations

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_OWNER = {"user": "di_owner", "password": "di_owner"}
_APP = {"user": "di_app", "password": "di_app"}


async def _connect(*, user: str, password: str) -> asyncpg.Connection:
    s = get_settings()
    return await asyncpg.connect(
        host=s.pg_host, port=s.pg_port, user=user, password=password, database=s.pg_database,
    )


async def _bind(conn: asyncpg.Connection, client_id: str | None) -> None:
    await conn.execute(
        "SELECT set_config('app.current_client_id', $1, false)", client_id or ""
    )


@pytest.fixture(scope="module")
async def schema() -> AsyncIterator[str]:
    """Apply migrations via the owner role; skip the module if the role split isn't provisioned."""
    s = get_settings()
    try:
        owner = await _connect(**_OWNER)
    except Exception as exc:  # noqa: BLE001 - any connect/auth failure means "not provisioned"
        pytest.skip(f"di_owner role unreachable (run docker/initdb or tools/bootstrap_roles.sql "
                    f"first): {exc}")
    try:
        await run_migrations(s, connection=owner)
    finally:
        await owner.close()
    yield s.pg_schema


async def _seed_tenant(owner: asyncpg.Connection, schema: str, client_id: str) -> None:
    """Insert one row into every tenant table for ``client_id``, as the owner.

    di_owner is deliberately NOT BYPASSRLS (005_hardening.sql: "FORCE so even the table owner is
    filtered" — an intentional design choice, not an oversight), so this binds the tenant GUC
    first. The owner is otherwise just another role for row-visibility purposes; only its schema
    ownership (DDL rights) is special.
    """
    await _bind(owner, client_id)
    doc_id = await owner.fetchval(
        f'INSERT INTO "{schema}".di_documents (id, client_id, document_name) '
        f"VALUES (gen_random_uuid(), $1, 'seed.pdf') RETURNING id",
        client_id,
    )
    ver_id = await owner.fetchval(
        f'INSERT INTO "{schema}".doc_version (id, client_id, doc_id, version_no, content_hash) '
        f"VALUES (gen_random_uuid(), $1, $2, 1, 'seedhash') RETURNING id",
        client_id, doc_id,
    )
    knode_id = await owner.fetchval(
        f'INSERT INTO "{schema}".knode '
        f"(id, client_id, doc_id, version_id, path, node_type, seq, depth) "
        f"VALUES (gen_random_uuid(), $1, $2, $3, 'root'::ltree, 'document', 0, 0) RETURNING id",
        client_id, doc_id, ver_id,
    )
    await owner.execute(
        f'INSERT INTO "{schema}".arep '
        f"(id, knode_id, client_id, doc_id, version_id, path, rep_type, rep_text) "
        f"VALUES (gen_random_uuid(), $1, $2, $3, $4, 'root'::ltree, 'summary', 'seed rep')",
        knode_id, client_id, doc_id, ver_id,
    )
    await owner.execute(
        f'INSERT INTO "{schema}".client_merged_fact (id, client_id, attribute_key, resolved_value) '
        f"VALUES (gen_random_uuid(), $1, 'id.test', 'seed')",
        client_id,
    )
    await owner.execute(
        f'INSERT INTO "{schema}".di_entity (id, client_id, entity_type) '
        f"VALUES (gen_random_uuid(), $1, 'person')",
        client_id,
    )
    await owner.execute(
        f'INSERT INTO "{schema}".di_decision_trace (id, client_id, doc_id) '
        f"VALUES (gen_random_uuid(), $1, $2)",
        client_id, doc_id,
    )
    await owner.execute(
        f'INSERT INTO "{schema}".di_job (id, client_id, status) '
        f"VALUES (gen_random_uuid(), $1, 'queued')",
        client_id,
    )
    await owner.execute(
        f'INSERT INTO "{schema}".di_blob (id, client_id, key, data) '
        f"VALUES (gen_random_uuid(), $1, 'seed-key', ''::bytea)",
        client_id,
    )
    await owner.execute(
        f'INSERT INTO "{schema}".di_fact_adjudication (id, client_id, attribute_key, verdict) '
        f"VALUES (gen_random_uuid(), $1, 'id.test', 'accept')",
        client_id,
    )
    await owner.execute(
        f'INSERT INTO "{schema}".di_fact_adjudication_event '
        f"(client_id, attribute_key, instance_key, verdict) VALUES ($1, 'id.test', '', 'accept')",
        client_id,
    )


async def _cleanup_tenant(owner: asyncpg.Connection, schema: str, client_id: str) -> None:
    """DELETE every seeded row, EXCEPT di_fact_adjudication_event: it is append-only by trigger
    (same as purge_client in di/store.py deliberately skips it) — a stray audit row from a
    randomly-generated test client_id is harmless and cannot be cleaned up by design."""
    await _bind(owner, client_id)
    for table in _TENANT_TABLES:
        if table == "di_fact_adjudication_event":
            continue
        await owner.execute(f'DELETE FROM "{schema}".{table} WHERE client_id = $1', client_id)


@pytest.fixture
async def two_tenants(schema: str) -> AsyncIterator[tuple[str, str]]:
    """Seed tenants A and B across every tenant table; clean up afterwards."""
    a, b = f"rls-a-{uuid.uuid4().hex[:8]}", f"rls-b-{uuid.uuid4().hex[:8]}"
    owner = await _connect(**_OWNER)
    try:
        await _seed_tenant(owner, schema, a)
        await _seed_tenant(owner, schema, b)
        yield a, b
    finally:
        await _cleanup_tenant(owner, schema, a)
        await _cleanup_tenant(owner, schema, b)
        await owner.close()


# ---------------------------------------------------------------------------
# 1. Cross-tenant reads return zero rows
# ---------------------------------------------------------------------------
async def test_cross_tenant_reads_are_isolated(schema: str, two_tenants: tuple[str, str]) -> None:
    a, b = two_tenants
    app_conn = await _connect(**_APP)
    # Control: di_owner is NOT BYPASSRLS either (005's "FORCE so even the table owner is
    # filtered"), so proving B's rows really exist means binding the owner connection AS B — it
    # is otherwise just another RLS-subject role. This still proves filtering, not absence: the
    # SAME owner connection sees B's rows when bound to B and (below) di_app sees none when bound
    # to A, so the difference is the policy, not a missing seed.
    owner_conn = await _connect(**_OWNER)
    try:
        await _bind(app_conn, a)
        await _bind(owner_conn, b)
        for table in _TENANT_TABLES:
            leaked = await app_conn.fetchval(
                f'SELECT count(*) FROM "{schema}".{table} WHERE client_id = $1', b,
            )
            assert leaked == 0, f"{table}: di_app bound to A could see B's rows"

            own = await app_conn.fetchval(
                f'SELECT count(*) FROM "{schema}".{table} WHERE client_id = $1', a,
            )
            assert own >= 1, f"{table}: di_app bound to A could not see its OWN rows"

            exists = await owner_conn.fetchval(
                f'SELECT count(*) FROM "{schema}".{table} WHERE client_id = $1', b,
            )
            assert exists >= 1, f"{table}: control seed for B is missing (test setup bug)"
    finally:
        await app_conn.close()
        await owner_conn.close()


# ---------------------------------------------------------------------------
# 2. Cross-tenant writes are rejected (WITH CHECK), not silently misdirected
# ---------------------------------------------------------------------------
async def test_cross_tenant_insert_is_rejected(schema: str, two_tenants: tuple[str, str]) -> None:
    a, b = two_tenants
    app_conn = await _connect(**_APP)
    try:
        await _bind(app_conn, a)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(
                f'INSERT INTO "{schema}".di_documents (id, client_id, document_name) '
                f"VALUES (gen_random_uuid(), $1, 'malicious.pdf')",
                b,  # bound as A, but the row claims to belong to B
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(
                f'INSERT INTO "{schema}".di_job (id, client_id, status) '
                f"VALUES (gen_random_uuid(), $1, 'queued')",
                b,
            )
    finally:
        await app_conn.close()


async def test_cross_tenant_update_and_delete_affect_zero_rows(
    schema: str, two_tenants: tuple[str, str],
) -> None:
    """UPDATE/DELETE targeting another tenant's rows are filtered by USING, not rejected outright
    — the command succeeds but the row count is 0, since the target rows are invisible."""
    a, b = two_tenants
    app_conn = await _connect(**_APP)
    try:
        await _bind(app_conn, a)
        tag = await app_conn.execute(
            f'UPDATE "{schema}".di_documents SET document_name = \'renamed\' WHERE client_id = $1',
            b,
        )
        assert tag.endswith(" 0"), f"UPDATE affected rows it should not see: {tag}"
        tag = await app_conn.execute(
            f'DELETE FROM "{schema}".di_documents WHERE client_id = $1', b,
        )
        assert tag.endswith(" 0"), f"DELETE affected rows it should not see: {tag}"
    finally:
        await app_conn.close()


# ---------------------------------------------------------------------------
# 3. No bound tenant -> zero rows (fail-closed, never fail-open)
# ---------------------------------------------------------------------------
async def test_unbound_connection_sees_nothing(schema: str, two_tenants: tuple[str, str]) -> None:
    a, _b = two_tenants
    app_conn = await _connect(**_APP)
    try:
        # current_client_id never set at all — the GUC is unset/empty for this connection.
        for table in _TENANT_TABLES:
            count = await app_conn.fetchval(f'SELECT count(*) FROM "{schema}".{table}')
            assert count == 0, f"{table}: unbound di_app connection saw rows"
    finally:
        await app_conn.close()


async def test_guc_does_not_leak_across_checkouts_on_a_reused_connection(
    schema: str, two_tenants: tuple[str, str],
) -> None:
    """Mirrors di.db.acquire()'s reset-on-release: binding then explicitly clearing the GUC on
    the SAME connection must return it to the fail-closed (zero-rows) state."""
    a, _b = two_tenants
    app_conn = await _connect(**_APP)
    try:
        await _bind(app_conn, a)
        assert await app_conn.fetchval(
            f'SELECT count(*) FROM "{schema}".di_documents WHERE client_id = $1', a) >= 1

        await app_conn.execute("SELECT set_config('app.current_client_id', '', false)")
        count = await app_conn.fetchval(f'SELECT count(*) FROM "{schema}".di_documents')
        assert count == 0, "clearing the GUC did not return the connection to fail-closed"
    finally:
        await app_conn.close()


# ---------------------------------------------------------------------------
# 4. Partitions cannot be queried directly to bypass the parent's RLS policy
# ---------------------------------------------------------------------------
async def test_direct_partition_select_is_denied(schema: str) -> None:
    """No tenant seed needed: partitions exist purely from migrations, and privilege denial does
    not depend on there being any rows."""
    app_conn = await _connect(**_APP)
    try:
        for table in ("knode", "arep"):
            partition = await app_conn.fetchval(
                "SELECT c.relname FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "JOIN pg_namespace n ON n.oid = p.relnamespace "
                "WHERE n.nspname = $1 AND p.relname = $2 ORDER BY c.relname LIMIT 1",
                schema, table,
            )
            assert partition is not None, f"{table} has no partitions — did migrations run?"
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await app_conn.fetch(f'SELECT * FROM "{schema}"."{partition}" LIMIT 1')
    finally:
        await app_conn.close()


# ---------------------------------------------------------------------------
# 5. Role posture: not superuser, not BYPASSRLS, narrowed global-table grants
# ---------------------------------------------------------------------------
async def test_runtime_role_is_not_superuser_or_bypassrls(schema: str) -> None:
    app_conn = await _connect(**_APP)
    try:
        row = await app_conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        assert row is not None
        assert row["rolsuper"] is False
        assert row["rolbypassrls"] is False
    finally:
        await app_conn.close()


async def test_di_app_cannot_delete_api_keys(schema: str) -> None:
    """Revocation is soft (UPDATE disabled_at, di/auth.py) — DELETE must be structurally denied."""
    app_conn = await _connect(**_APP)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(f'DELETE FROM "{schema}".di_api_key WHERE false')
    finally:
        await app_conn.close()


async def test_di_app_cannot_write_the_migration_ledger(schema: str) -> None:
    """Only the owner/migration role may record applied migrations."""
    app_conn = await _connect(**_APP)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(
                f'INSERT INTO "{schema}".di_migration_ledger (filename, checksum) '
                f"VALUES ('fake.sql', 'deadbeef')"
            )
    finally:
        await app_conn.close()


# ---------------------------------------------------------------------------
# 6. assert_rls_posture (the actual boot-time guard) reports a clean posture
# ---------------------------------------------------------------------------
async def test_assert_rls_posture_reports_clean_against_the_live_schema(schema: str) -> None:
    """End-to-end proof that di.db.assert_rls_posture — what actually runs at boot — is clean
    once the role split + 006 are applied, connecting as the runtime role would."""
    import di.db as db_module

    settings = get_settings()
    original_user, original_password = settings.pg_user, settings.pg_password
    original_rls = settings.rls_enabled
    try:
        settings.pg_user, settings.pg_password = _APP["user"], _APP["password"]
        settings.rls_enabled = True
        await db_module.close_pool()
        violations = await db_module.assert_rls_posture(settings)
        assert violations == [], f"unexpected RLS posture violations: {violations}"
    finally:
        settings.pg_user, settings.pg_password = original_user, original_password
        settings.rls_enabled = original_rls
        await db_module.close_pool()
