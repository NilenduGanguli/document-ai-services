-- docker/initdb/01_roles.sql — runs automatically by the postgres image's entrypoint on FIRST
-- init of an empty data directory (docker-entrypoint-initdb.d convention), as the bootstrap
-- superuser (POSTGRES_USER). This is the fresh-database counterpart of tools/bootstrap_roles.sql
-- (which is for an EXISTING database that predates the role split — it additionally reassigns
-- ownership of objects that already exist, which is meaningless here since nothing exists yet).
--
-- Passwords are demo-only literals, matching the existing compose convention (PG_PASSWORD: di in
-- docker-compose.yml). Production deployments source real passwords from a secret manager and set
-- them out-of-band — see docs/specs/2026-07-15-enterprise-scale-plan.md §3 "Ops runbook".

CREATE ROLE di_owner LOGIN PASSWORD 'di_owner'
    NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;

CREATE ROLE di_app_rw NOLOGIN NOSUPERUSER NOBYPASSRLS;
CREATE ROLE di_worker NOLOGIN NOSUPERUSER NOBYPASSRLS;

CREATE ROLE di_app LOGIN PASSWORD 'di_app'
    NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
-- Reserved for the durable-queue upgrade; created now so that upgrade needs no second bootstrap
-- pass or compose change beyond pointing the worker service at these credentials.
CREATE ROLE di_worker_login LOGIN PASSWORD 'di_worker'
    NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

GRANT di_app_rw TO di_app;
GRANT di_app_rw, di_worker TO di_worker_login;

-- di_owner runs every migration (CREATE SCHEMA, CREATE EXTENSION for ltree/pgcrypto, all DDL). A
-- plain GRANT CREATE ON DATABASE does NOT transitively grant CREATE on schema public on PG15+
-- (that requires being the database owner — tracked separately via pg_database_owner — or an
-- explicit schema grant); without one of those, 001_extensions.sql's ltree/pgcrypto CREATE
-- EXTENSION (into the default public schema) would be refused. Make di_owner the actual database
-- owner so it has CREATE everywhere it needs it, including schema public, by construction.
GRANT CONNECT ON DATABASE document_intelligence TO di_owner;
ALTER DATABASE document_intelligence OWNER TO di_owner;

-- pgvector is a SEPARATE case: unlike ltree/pgcrypto, this image's `vector` extension control
-- file is not marked trusted, so "CREATE EXTENSION vector" unconditionally requires an actual
-- superuser — no schema or database ownership grant changes that (verified directly: di_owner
-- gets "ERROR: permission denied to create extension vector / HINT: Must be superuser"). Only the
-- bootstrap superuser (this script's own role, POSTGRES_USER) can install it; di/db.py's runtime
-- bootstrap (`CREATE EXTENSION IF NOT EXISTS vector`) then finds it already present and no-ops,
-- exactly like it already does for ltree/pgcrypto. Best-effort: some Postgres images genuinely
-- lack pgvector, and the app already degrades that to full-text-only search cleanly.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pgvector extension unavailable on this Postgres image; skipping (%).', SQLERRM;
END$$;
