"""Static invariants of the SQL migrations — with a focus on 005_hardening.sql.

Pure-logic (no DB / no network): every assertion is a parse of the .sql text on disk.

These guard the failure modes that a live boot would NOT surface loudly. di/db.py:run_migrations
re-applies every file on each startup, so a lost ``IF NOT EXISTS`` crashes the app on the *second*
boot, and a hardcoded schema silently writes to the wrong one. The DB-backed proof that the SQL is
valid PostgreSQL lives in the migration run itself; this file keeps the idioms from regressing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from di.config import get_settings

_MIGRATIONS = Path(__file__).parent.parent / "di" / "migrations"
_HARDENING = _MIGRATIONS / "005_hardening.sql"
_VERSION_BLOB = _MIGRATIONS / "012_doc_version_blob.sql"

#: tenant-scoped tables added by 005 — each MUST carry a tenant_isolation policy.
_TENANT_TABLES = ("di_job", "di_blob", "di_fact_adjudication")
#: global tables added by 005 — read before/outside any tenant context, so RLS would break them.
_GLOBAL_TABLES = ("di_api_key", "di_migration_ledger")


@pytest.fixture(scope="module")
def sql() -> str:
    """The raw text of 005_hardening.sql."""
    return _HARDENING.read_text(encoding="utf-8")


def test_hardening_migration_exists(sql: str) -> None:
    assert sql.strip(), "005_hardening.sql must not be empty"


def test_schema_is_never_hardcoded(sql: str) -> None:
    """Every relation must be qualified with the __SCHEMA__ token db.py rewrites."""
    configured = get_settings().pg_schema
    assert f'"{configured}".' not in sql, f"hardcoded schema {configured!r} — use __SCHEMA__"
    assert "__SCHEMA__." in sql


@pytest.mark.parametrize("path", sorted(_MIGRATIONS.glob("*.sql")), ids=lambda p: p.name)
def test_no_psql_meta_commands(path: Path) -> None:
    """run_migrations does a plain conn.execute() — psql meta-commands would be a syntax error."""
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        assert not re.match(r"^\\[a-z]", stripped), f"{path.name}:{lineno} psql meta-command"


@pytest.mark.parametrize("path", sorted(_MIGRATIONS.glob("*.sql")), ids=lambda p: p.name)
def test_creates_are_idempotent(path: Path) -> None:
    """Migrations re-run on every boot: CREATE TABLE/INDEX/SEQUENCE need IF NOT EXISTS.

    Statements inside DO $$ blocks are exempt — 004/005 guard those procedurally (DROP ... IF
    EXISTS then CREATE), which CREATE POLICY has no IF NOT EXISTS form to express.
    """
    text = re.sub(r"DO \$\$.*?END\$\$;", "", path.read_text(encoding="utf-8"), flags=re.S)
    pattern = re.compile(
        r"CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX|SEQUENCE)\s+(?!IF\s+NOT\s+EXISTS)", re.I
    )
    assert not pattern.findall(text), f"{path.name}: CREATE without IF NOT EXISTS"


def test_existing_tables_are_altered_not_recreated(sql: str) -> None:
    """002's tables already exist in every deployed DB, so CREATE TABLE IF NOT EXISTS would be a
    silent no-op there. New columns must arrive via ADD COLUMN IF NOT EXISTS."""
    expected = {
        "di_decision_trace": ("rationale", "anchor_summary"),
        "client_merged_fact": (
            "verification_status",
            "winning_fact_id",
            "resolution_rationale",
            "ontology_version",
            "adjudicated",
        ),
        "di_documents": ("external_document_id", "blob_uri", "blob_backend"),
        "doc_version": ("change_seq",),
    }
    for table, columns in expected.items():
        assert f"CREATE TABLE IF NOT EXISTS __SCHEMA__.{table}" not in sql, (
            f"{table} is an existing (002) table — ALTER it, do not re-CREATE it"
        )
        block = re.search(
            rf"ALTER TABLE __SCHEMA__\.{table}\s+(ADD COLUMN.*?);", sql, re.S | re.I
        )
        assert block, f"no ALTER TABLE ... ADD COLUMN block for {table}"
        for col in columns:
            assert re.search(rf"ADD COLUMN IF NOT EXISTS\s+{col}\b", block.group(1)), (
                f"{table}.{col} missing or not guarded by IF NOT EXISTS"
            )


def test_new_tables_are_created(sql: str) -> None:
    for table in (*_TENANT_TABLES, *_GLOBAL_TABLES):
        assert f"CREATE TABLE IF NOT EXISTS __SCHEMA__.{table}" in sql, f"{table} not created"


def _rls_tables(sql: str) -> set[str]:
    """The table names listed in 005's RLS DO-block array."""
    block = re.search(r"tables text\[\] := ARRAY\[(.*?)\];", sql, re.S)
    assert block, "no RLS DO-block found"
    return set(re.findall(r"'([^']+)'", block.group(1)))


def test_rls_covers_every_new_tenant_table(sql: str) -> None:
    assert _rls_tables(sql) >= set(_TENANT_TABLES)


def test_rls_excludes_global_tables(sql: str) -> None:
    """di_api_key is looked up BEFORE a client is known (it maps a key -> client_ids), and the
    ledger is infra state. A tenant_isolation policy on either returns zero rows and locks out
    auth entirely."""
    for table in _GLOBAL_TABLES:
        assert table not in _rls_tables(sql), f"{table} is global — it must not get an RLS policy"
        # \b so di_api_key.client_ids (the plural allow-list) is not mistaken for a tenant column.
        assert not re.search(r"\bclient_id\b", _table_body(sql, table)), (
            f"{table} must not have a client_id column"
        )


def _table_body(sql: str, table: str) -> str:
    body = re.search(
        rf"CREATE TABLE IF NOT EXISTS __SCHEMA__\.{table} \((.*?)\n\);", sql, re.S
    )
    assert body, f"cannot parse body of {table}"
    return body.group(1)


def test_rls_policy_matches_004_pattern(sql: str) -> None:
    """Same GUC + ENABLE/FORCE/DROP/CREATE shape as 004_rls.sql, or tenants leak."""
    for fragment in (
        "ENABLE ROW LEVEL SECURITY",
        "FORCE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS tenant_isolation",
        "CREATE POLICY tenant_isolation",
        "current_setting(''app.current_client_id'', true)",
    ):
        assert fragment in sql, f"RLS block missing {fragment!r}"


def test_tenant_tables_have_client_id(sql: str) -> None:
    """An RLS policy referencing client_id fails to create if the column is absent."""
    for table in _TENANT_TABLES:
        assert re.search(r"client_id\s+text NOT NULL", _table_body(sql, table)), table


def test_idempotency_and_external_id_uniques_are_partial(sql: str) -> None:
    """NULL idempotency keys / external ids are the common case and must never collide."""
    for index, column in (
        ("di_job_client_idem", "idempotency_key"),
        ("di_documents_client_extid", "external_document_id"),
    ):
        stmt = re.search(rf"CREATE UNIQUE INDEX IF NOT EXISTS {index}(.*?);", sql, re.S)
        assert stmt, f"{index} missing"
        assert f"WHERE {column} IS NOT NULL" in stmt.group(1), f"{index} must be partial"


def test_change_seq_default_is_wired_to_the_sequence(sql: str) -> None:
    """The column is useless as a change-feed cursor without the nextval default."""
    assert "CREATE SEQUENCE IF NOT EXISTS __SCHEMA__.doc_version_change_seq;" in sql
    assert "ALTER COLUMN change_seq SET DEFAULT nextval('__SCHEMA__.doc_version_change_seq')" in sql


def test_sequence_is_created_before_it_is_referenced(sql: str) -> None:
    """The whole file executes as one batch — a SET DEFAULT above the CREATE SEQUENCE fails."""
    assert sql.index("CREATE SEQUENCE IF NOT EXISTS __SCHEMA__.doc_version_change_seq") < sql.index(
        "SET DEFAULT nextval('__SCHEMA__.doc_version_change_seq')"
    )


def test_change_seq_index_does_not_collide_with_the_sequence(sql: str) -> None:
    """Indexes and sequences share pg_class: an index named doc_version_change_seq would clash."""
    assert "CREATE INDEX IF NOT EXISTS doc_version_change_seq " not in sql
    assert "CREATE INDEX IF NOT EXISTS doc_version_client_change_seq" in sql


# ---------------------------------------------------------------------------
# 012_doc_version_blob.sql — the per-version payload locator
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def blob_sql() -> str:
    """The raw text of 012_doc_version_blob.sql."""
    return _VERSION_BLOB.read_text(encoding="utf-8")


def test_012_alters_doc_version_rather_than_recreating_it(blob_sql: str) -> None:
    """doc_version is a 002 table and exists in every deployed DB, so CREATE TABLE IF NOT EXISTS
    would be a silent no-op there and the columns would never appear."""
    assert "CREATE TABLE" not in blob_sql.upper()
    assert "ALTER TABLE __SCHEMA__.doc_version" in blob_sql


@pytest.mark.parametrize("column", ["blob_uri", "blob_backend"])
def test_012_columns_are_guarded_by_if_not_exists(blob_sql: str, column: str) -> None:
    """run_migrations re-applies every file on each boot — an unguarded ADD COLUMN crashes the
    app on the second boot."""
    assert re.search(rf"ADD COLUMN IF NOT EXISTS\s+{column}\s+text\b", blob_sql), column


def test_012_schema_is_never_hardcoded(blob_sql: str) -> None:
    """Same rule as 005: the __SCHEMA__ token is what db.py rewrites per deployment."""
    configured = get_settings().pg_schema
    assert f'"{configured}".' not in blob_sql, f"hardcoded schema {configured!r} — use __SCHEMA__"
    assert "__SCHEMA__." in blob_sql


def test_012_is_pure_ddl(blob_sql: str) -> None:
    """A back-fill of the new columns would be DML, and 004 has already FORCE'd RLS by the time
    migrations run — it would be silently filtered to zero rows under the production role."""
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in blob_sql.upper().replace("-- ", ""), f"{verb.strip()} is unsafe here"


def test_012_columns_match_the_di_documents_pair(sql: str, blob_sql: str) -> None:
    """The version-level pointer must use the SAME column names/types 005 gave di_documents —
    di/store.py reads both through one DocumentMeta shape, and a rename would split the story."""
    for column in ("blob_uri", "blob_backend"):
        assert re.search(rf"ADD COLUMN IF NOT EXISTS\s+{column}\s+text\b", sql), (
            f"di_documents.{column} is the 005 precedent this file mirrors"
        )
        assert re.search(rf"ADD COLUMN IF NOT EXISTS\s+{column}\s+text\b", blob_sql), column


def test_migration_is_pure_ddl(sql: str) -> None:
    """004 has already FORCE'd RLS by the time 005 runs, so DML here is silently filtered to zero
    rows under the non-superuser role production uses. Back-fills belong in app code that binds
    the tenant GUC."""
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in sql.upper().replace("-- ", ""), f"{verb.strip()} is unsafe under FORCE RLS"
