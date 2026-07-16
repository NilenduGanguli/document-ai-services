-- 007_auth_hardening.sql — key lifecycle, tenant policy, read-side access audit. Idempotent.
-- Runs under the migration/owner role (di_owner), same as every other migration.
--
-- Why each piece exists:
--   di_api_key columns   — expiry + rotation lineage so keys are not immortal by default, and a
--                          per-key rate-limit override for tenants that need a different budget
--                          than the fleet default.
--   di_tenant_policy     — per-tenant operational limits (ingest quotas). GLOBAL like di_api_key:
--                          read at admission time, before any tenant GUC is bound, and
--                          administered cross-tenant, so no RLS policy.
--   di_access_log        — append-only, monthly-partitioned read-side audit trail. Answers "who
--                          read this client's data, and did they see it masked or unmasked?" —
--                          previously unanswerable (only mutations left a trace). GLOBAL (no RLS):
--                          rows are written in batches spanning many tenants on one connection by
--                          a background writer, and read only via the admin-scoped query endpoint
--                          (mirrors di_api_key's rationale, 005_hardening.sql:28-29).
--   rotated_from partial  — defense-in-depth alongside the application-level SELECT ... FOR UPDATE
--   unique index          in the rotate endpoint (di/auth.py): guards against two live successor
--                          keys ever pointing at the same rotated-from parent.
--   Explicit grants        — following 006's convention: no ALL TABLES, no ALTER DEFAULT
--                          PRIVILEGES for tables. Every migration that adds a table grants it here.

-- ------------------------------------------------------------------------------------------
-- Key lifecycle
-- ------------------------------------------------------------------------------------------
ALTER TABLE __SCHEMA__.di_api_key
    ADD COLUMN IF NOT EXISTS expires_at     timestamptz,
    ADD COLUMN IF NOT EXISTS rotated_from   uuid,
    ADD COLUMN IF NOT EXISTS rate_limit_rps integer,
    ADD COLUMN IF NOT EXISTS created_by     text;

CREATE UNIQUE INDEX IF NOT EXISTS di_api_key_rotated_from_live
    ON __SCHEMA__.di_api_key (rotated_from) WHERE disabled_at IS NULL;

CREATE INDEX IF NOT EXISTS di_api_key_expires_at
    ON __SCHEMA__.di_api_key (expires_at) WHERE expires_at IS NOT NULL;

-- ------------------------------------------------------------------------------------------
-- Per-tenant operational policy (ingest quotas). GLOBAL, no RLS — see header.
-- ------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_tenant_policy (
    client_id          text PRIMARY KEY,
    max_active_jobs    integer,          -- NULL = settings default
    daily_ingest_limit integer,          -- NULL = settings default; 0 = blocked
    note               text,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------------------------------
-- Read-side access audit. Append-only, RANGE-partitioned by month on ts. GLOBAL, no RLS.
-- ------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_access_log (
    id         bigint GENERATED ALWAYS AS IDENTITY,
    ts         timestamptz NOT NULL DEFAULT now(),
    key_id     text,
    principal  text,
    client_id  text,
    method     text NOT NULL,
    route      text NOT NULL,           -- route TEMPLATE (e.g. /api/v1/clients/{client_id}/facts)
    status     smallint NOT NULL,
    masked     boolean,                 -- serving projection: was the masked view returned?
    request_id text,
    extra      jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (ts, id)                -- partition key must be in the PK
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS di_access_log_client_ts ON __SCHEMA__.di_access_log (client_id, ts DESC);
CREATE INDEX IF NOT EXISTS di_access_log_key_ts    ON __SCHEMA__.di_access_log (key_id, ts DESC);

-- Append-only enforcement: block UPDATE/DELETE at the trigger level. Partition DROP/DETACH is DDL
-- (run by the owner role during retention export, di/tools/audit_export.py) and is unaffected.
CREATE OR REPLACE FUNCTION __SCHEMA__.di_access_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'di_access_log is append-only';
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS di_access_log_no_rewrite ON __SCHEMA__.di_access_log;
CREATE TRIGGER di_access_log_no_rewrite
    BEFORE UPDATE OR DELETE ON __SCHEMA__.di_access_log
    FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.di_access_log_immutable();

-- Partition bootstrap lives in di/db.py:ensure_access_log_partitions (called from run_migrations,
-- under the owner role and the advisory lock) rather than here: the horizon is a runtime setting
-- (access_audit_partition_months_ahead), and a fixed migration file cannot express "N months from
-- whenever this actually runs" without becoming non-idempotent text. This migration only creates
-- the parent (above); di/db.py creates today's partition immediately after so the table is
-- writable even before the app finishes its own first-boot partition-horizon check.
DO $$
DECLARE m date := date_trunc('month', now())::date;
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS __SCHEMA__.%I PARTITION OF __SCHEMA__.di_access_log '
        'FOR VALUES FROM (%L) TO (%L);',
        'di_access_log_' || to_char(m, 'YYYY_MM'), m, m + interval '1 month');
END$$;

-- ------------------------------------------------------------------------------------------
-- Grants (explicit, per the 006 convention — no ALL TABLES, no default privileges).
-- ------------------------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON __SCHEMA__.di_tenant_policy TO di_app_rw;
GRANT SELECT, INSERT ON __SCHEMA__.di_access_log TO di_app_rw;
GRANT USAGE, SELECT ON __SCHEMA__.di_access_log_id_seq TO di_app_rw;
