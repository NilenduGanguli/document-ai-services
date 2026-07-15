-- tools/bootstrap_roles.sql — ONE-TIME superuser step for the RLS production-posture upgrade.
--
-- Run this ONCE, as a superuser, against any EXISTING (already-bootstrapped) database before
-- deploying the release that includes di/migrations/006_roles_and_grants.sql. It is NOT a
-- ledgered migration and cannot be one: di_owner cannot REASSIGN OWNED from itself, and role
-- creation + ownership transfer require superuser (or CREATEROLE + membership) privileges the
-- runtime and migration roles deliberately never hold.
--
-- A FRESH database (a brand-new `docker compose up` after `down -v`) does NOT need this file —
-- docker/initdb/01_roles.sql creates the same roles at container-init time, before the app ever
-- connects, so there is nothing to reassign. Use this script only for a database that already has
-- objects owned by an older single bootstrap role (e.g. the compose demo's `di` user, or any
-- hand-provisioned environment).
--
-- Usage:
--   psql "postgresql://<superuser>@<host>:<port>/<database>" \
--        -v old_owner=di -v db_name=document_intelligence \
--        -v owner_pw='<choose-a-strong-password>' -v app_pw='<choose-a-strong-password>' \
--        -f tools/bootstrap_roles.sql
--
-- Passwords are psql variables so they never live in this file. Idempotent: safe to re-run.

\set ON_ERROR_STOP on

-- --------------------------------------------------------------------------------------------
-- 1. Create the roles (idempotent — DO blocks swallow duplicate_object).
-- --------------------------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'di_owner') THEN
        EXECUTE format(
            'CREATE ROLE di_owner LOGIN PASSWORD %L '
            'NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT',
            :'owner_pw'
        );
    END IF;
END$$;

-- Group roles (NOLOGIN): grants live here, not on individual login roles, so rotating a login
-- role is a membership change, never a re-migration.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'di_app_rw') THEN
        CREATE ROLE di_app_rw NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'di_worker') THEN
        CREATE ROLE di_worker NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END$$;

-- Login roles. di_app is the API's runtime role; di_worker_login is reserved now for the
-- durable-queue upgrade (phase 4) so it never requires a second bootstrap pass.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'di_app') THEN
        EXECUTE format(
            'CREATE ROLE di_app LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
            :'app_pw'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'di_worker_login') THEN
        EXECUTE format(
            'CREATE ROLE di_worker_login LOGIN PASSWORD %L '
            'NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
            :'app_pw'
        );
    END IF;
END$$;

GRANT di_app_rw TO di_app;
GRANT di_app_rw, di_worker TO di_worker_login;

-- di_owner needs to be the actual DATABASE OWNER, not merely GRANTed CREATE on it: a plain GRANT
-- CREATE ON DATABASE does NOT transitively grant CREATE on schema public on PG15+ (that requires
-- ownership, tracked separately via pg_database_owner, or an explicit schema-level grant), so
-- 001_extensions.sql's ltree/pgcrypto CREATE EXTENSION (into the default public schema) would be
-- silently refused otherwise. ALTER DATABASE OWNER TO is safe on a database that already has
-- objects: it only changes who is tracked as the owning role.
GRANT CONNECT ON DATABASE :"db_name" TO di_owner;
ALTER DATABASE :"db_name" OWNER TO di_owner;

-- pgvector is a SEPARATE case from ltree/pgcrypto above: this image's `vector` extension control
-- file is not marked trusted, so CREATE EXTENSION unconditionally requires an actual superuser —
-- no ownership grant changes that (verified: di_owner gets "ERROR: permission denied to create
-- extension vector / HINT: Must be superuser"). Only this script's own (superuser) connection can
-- install it; di/db.py's runtime bootstrap then finds it already present and no-ops. Best-effort:
-- some Postgres installs genuinely lack pgvector, and the app degrades that to full-text-only
-- search cleanly.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pgvector extension unavailable; skipping (%).', SQLERRM;
END$$;

-- --------------------------------------------------------------------------------------------
-- 2. Transfer ownership of every existing object from the old bootstrap role to di_owner.
-- --------------------------------------------------------------------------------------------
-- Idempotent: re-running REASSIGN OWNED on a role that owns nothing is a no-op. Must run AFTER
-- the ALTER DATABASE OWNER above: REASSIGN OWNED transfers objects the CURRENT SESSION's role can
-- see and has rights to reassign, and connecting as a superuser covers both regardless of order,
-- but keeping database ownership and object ownership transfer adjacent makes the script's intent
-- unambiguous on read.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'old_owner') THEN
        EXECUTE format('REASSIGN OWNED BY %I TO di_owner', :'old_owner');
    END IF;
END$$;

-- --------------------------------------------------------------------------------------------
-- 3. Report.
-- --------------------------------------------------------------------------------------------

SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
FROM pg_roles
WHERE rolname IN ('di_owner', 'di_app_rw', 'di_worker', 'di_app', 'di_worker_login')
ORDER BY rolname;

\echo 'Done. Next: set PG_USER=di_app, PG_MIGRATION_USER=di_owner (+ their passwords), RLS_ENABLED=true,'
\echo 'MIGRATIONS_MODE=auto in the app environment, then deploy the release containing 006_roles_and_grants.sql.'
