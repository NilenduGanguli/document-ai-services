-- 006_roles_and_grants.sql — least-privilege runtime roles for the RLS production posture.
-- Idempotent. Runs as the migration/owner role (di_owner) — see di/db.py:open_migration_connection.
--
-- Why each piece exists:
--   di_app_rw role   — NOLOGIN group role every runtime login role (di_app, and later
--                      di_worker_login) is a MEMBER of. Grants live on this fixed group role, not
--                      on a templated/rotatable login-role name: a PG_USER rotation is then a
--                      membership change, never a re-migration (the checksum ledger would skip a
--                      re-run of this file on an unrelated role rename, so a name baked into the
--                      SQL would silently stop granting anything to the new role).
--   di_worker role   — NOLOGIN group role reserved now for the durable-queue upgrade's worker
--                      pool. Its claim policy on di_job ships in THIS migration (see below) even
--                      though no worker exists yet, so the posture guard and any security review
--                      see the final RLS shape once instead of twice.
--   Explicit table    — DML is granted to an explicit list of the 10 tenant tables, never
--   list, no ALL       "ALL TABLES IN SCHEMA" and never ALTER DEFAULT PRIVILEGES for tables. Both
--   TABLES/defaults     of those would silently hand di_app_rw direct access to knode_p*/arep_p*
--                      hash partitions — RLS policies exist only on the PARENT tables (004/005),
--                      and Postgres checks privileges on the exact relation named in a query, so
--                      "SELECT * FROM knode_p3" as di_app_rw would return every tenant's rows.
--                      Since partitions are never named in this file (or any future one), they
--                      get zero privileges by construction — no partition-hole to close in code.
--                      Every future migration that adds a table grants it explicitly, following
--                      this file's pattern; di/db.py:assert_rls_posture additionally verifies at
--                      every boot that no partition has leaked a direct grant.
--   worker_claim      — di_job needs a cross-tenant claim query (`FOR UPDATE SKIP LOCKED` with no
--   policy              tenant GUC bound) once the durable-queue upgrade lands. A role-targeted
--                      permissive policy is the correct primitive — NOT a GUC-trust policy like
--                      `current_setting('app.job_queue_worker') = 'on'`, which any di_app_rw
--                      session could set for itself. Permissive policies for the SAME command on
--                      the SAME table are OR-combined, so this coexists with tenant_isolation
--                      without weakening it: a di_app session (not a member of di_worker) never
--                      matches this policy at all.

-- --------------------------------------------------------------------------------------------
-- 1. Roles (belt-and-braces: docker/initdb/01_roles.sql or tools/bootstrap_roles.sql normally
--    create these first; this file must still be safe to run first on a database that skipped
--    that step, since MIGRATIONS_MODE=auto runs it automatically).
-- --------------------------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'di_app_rw') THEN
        CREATE ROLE di_app_rw NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'di_worker') THEN
        CREATE ROLE di_worker NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END$$;

-- --------------------------------------------------------------------------------------------
-- 2. Base privileges. Nobody gets implicit access.
-- --------------------------------------------------------------------------------------------

-- NOTE: di_worker membership is granted ONLY to di_worker_login (the worker pool's login role),
-- in docker/initdb/01_roles.sql / tools/bootstrap_roles.sql — never here, and never to di_app_rw.
-- Granting di_worker to di_app_rw would make every di_app_rw member (including the plain API
-- login role di_app) a transitive member of di_worker, so ordinary API requests would match the
-- worker_claim policy below and gain cross-tenant read/write on di_job — exactly the hole the
-- role split exists to prevent. Keep the two group roles' memberships disjoint.
REVOKE ALL ON SCHEMA __SCHEMA__ FROM PUBLIC;
GRANT USAGE ON SCHEMA __SCHEMA__ TO di_app_rw;
GRANT USAGE ON SCHEMA __SCHEMA__ TO di_worker;

-- --------------------------------------------------------------------------------------------
-- 3. Explicit per-table DML grants — the 10 tenant tables (004_rls.sql + 005_hardening.sql).
-- --------------------------------------------------------------------------------------------

GRANT SELECT, INSERT, UPDATE, DELETE ON
    __SCHEMA__.di_documents,
    __SCHEMA__.doc_version,
    __SCHEMA__.di_entity,
    __SCHEMA__.client_merged_fact,
    __SCHEMA__.di_decision_trace,
    __SCHEMA__.knode,
    __SCHEMA__.arep,
    __SCHEMA__.di_job,
    __SCHEMA__.di_blob,
    __SCHEMA__.di_fact_adjudication
TO di_app_rw;

-- Global tables, narrowed per how the app actually uses them (di/auth.py, di/db.py):
--   di_api_key          — no DELETE: revocation is soft (UPDATE disabled_at), never a hard delete.
--   di_migration_ledger  — SELECT only: written exclusively by the migration/owner role, read by
--                          the runtime role's verify_migrations (MIGRATIONS_MODE=verify).
GRANT SELECT, INSERT, UPDATE ON __SCHEMA__.di_api_key TO di_app_rw;
GRANT SELECT ON __SCHEMA__.di_migration_ledger TO di_app_rw;

-- nextval() on doc_version_change_seq (005_hardening.sql) and any future sequence the app touches
-- is granted explicitly per-migration, matching the "no blanket defaults" convention above.
GRANT USAGE, SELECT ON __SCHEMA__.doc_version_change_seq TO di_app_rw;

-- --------------------------------------------------------------------------------------------
-- 4. Reserve the worker claim policy now (durable-queue upgrade consumes it later).
-- --------------------------------------------------------------------------------------------

DROP POLICY IF EXISTS worker_claim ON __SCHEMA__.di_job;
CREATE POLICY worker_claim ON __SCHEMA__.di_job
    TO di_worker
    USING (true)
    WITH CHECK (true);
