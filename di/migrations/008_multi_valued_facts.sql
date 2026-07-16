-- 008_multi_valued_facts.sql — multi-cardinality attributes (directors, beneficial owners,
-- authorized signers, accounts): per-instance merged rows, per-instance adjudication, and an
-- append-only adjudication history. Idempotent; pure DDL (005's note applies here too: DML under
-- the runner would be RLS-filtered under the non-superuser app role — backfill happens via
-- application re-merge, which binds the tenant GUC). Runs under the migration/owner role
-- (di_owner) via the same advisory-locked startup path as every other migration, so it works
-- unchanged after the Phase-1 RLS role split (di_owner holds the table ownership ALTER TABLE
-- needs; the least-privilege runtime role di_app never runs migrations).
--
-- instance_key is '' (sentinel, never NULL — NULL would break the unique index and the upsert
-- conflict target) for single-valued attributes, so their uniqueness and upsert behavior are
-- byte-identical to before. Existing rows adopt instance_key '' via the column DEFAULT; the first
-- re-merge of a client rewrites multi-key rows with real fingerprints and deletes the stale ''
-- row (di/store.py::replace_merged_facts).
--
-- DEPLOY PATH: this ships as a single migration (not a two-release 008/009 split behind a feature
-- flag). Accepted rolling-deploy risk: once this file drops the old UNIQUE(client_id,
-- attribute_key) constraint, an old app replica's ON CONFLICT (client_id, attribute_key) raises
-- "no unique constraint matching" on the merge stage of an in-flight ingest — the job fails
-- retriably (di_job events) and re-merge is idempotent from source facts. For this project's
-- single-instance compose/demo deployment target this window does not apply; a multi-replica
-- production rollout should drain old replicas before/during this migration rather than rolling.
--
-- SCALE OPS NOTE: at tens-of-millions-of-rows scale, the non-concurrent unique-index builds below
-- take a SHARE lock blocking writes for the build, and this whole file runs inside the startup
-- path under di.db's advisory lock as one multi-statement implicit transaction — a
-- readiness/liveness timeout could kill the pod mid-build and crash-loop the deploy. For a large
-- deployment, build both unique indexes out-of-band (CONCURRENTLY, outside this migration runner)
-- before rolling this release; the IF NOT EXISTS guards below then make this file's index
-- creation a no-op. This project's current scale does not warrant that out-of-band step.

ALTER TABLE __SCHEMA__.client_merged_fact
    ADD COLUMN IF NOT EXISTS instance_key text NOT NULL DEFAULT '';

-- Create the replacement uniqueness BEFORE dropping the old one: no window without a uniqueness
-- guarantee, and the new index is the ON CONFLICT arbiter for the new writer.
CREATE UNIQUE INDEX IF NOT EXISTS client_merged_fact_client_attr_instance
    ON __SCHEMA__.client_merged_fact (client_id, attribute_key, instance_key);

-- Auto-generated name from 002_core_tables.sql's UNIQUE (client_id, attribute_key); confirmed
-- against a live pre-008 database during implementation.
ALTER TABLE __SCHEMA__.client_merged_fact
    DROP CONSTRAINT IF EXISTS client_merged_fact_client_id_attribute_key_key;

ALTER TABLE __SCHEMA__.di_fact_adjudication
    ADD COLUMN IF NOT EXISTS instance_key text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS updated_at   timestamptz NOT NULL DEFAULT now();

CREATE UNIQUE INDEX IF NOT EXISTS di_fact_adjudication_client_attr_instance
    ON __SCHEMA__.di_fact_adjudication (client_id, attribute_key, instance_key);

-- Auto-generated name from 005_hardening.sql's UNIQUE (client_id, attribute_key); confirmed
-- against a live pre-008 database during implementation.
ALTER TABLE __SCHEMA__.di_fact_adjudication
    DROP CONSTRAINT IF EXISTS di_fact_adjudication_client_id_attribute_key_key;

-- ------------------------------------------------------------------------------------------
-- Append-only adjudication history. di_fact_adjudication (above) stays the LIVE, mutable verdict
-- per (client_id, attribute_key, instance_key) — a second verdict overwrites it, which is exactly
-- what a bank's compliance record must never do silently. This table is the durable audit trail:
-- every write to di_fact_adjudication (including clears) also appends one row here, in the same
-- transaction (di/store.py::upsert_adjudication / clear_adjudication).
-- ------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_fact_adjudication_event (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id     text NOT NULL,
    attribute_key text NOT NULL,
    instance_key  text NOT NULL DEFAULT '',
    verdict       text NOT NULL CHECK (verdict IN ('accept', 'reject', 'override', 'cleared')),
    value_text    text,
    value_date    date,
    value_num     double precision,
    reviewer      text,
    note          text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS di_fact_adjudication_event_client
    ON __SCHEMA__.di_fact_adjudication_event (client_id, attribute_key, instance_key, created_at DESC);

CREATE OR REPLACE FUNCTION __SCHEMA__.di_fact_adjudication_event_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'di_fact_adjudication_event is append-only';
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS di_fact_adjudication_event_no_rewrite ON __SCHEMA__.di_fact_adjudication_event;
CREATE TRIGGER di_fact_adjudication_event_no_rewrite
    BEFORE UPDATE OR DELETE ON __SCHEMA__.di_fact_adjudication_event
    FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.di_fact_adjudication_event_immutable();

-- RLS: same tenant_isolation pattern as every other per-client table (004_rls.sql /
-- 005_hardening.sql). client_merged_fact and di_fact_adjudication already have it; only the new
-- table needs it here.
ALTER TABLE __SCHEMA__.di_fact_adjudication_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE __SCHEMA__.di_fact_adjudication_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON __SCHEMA__.di_fact_adjudication_event;
CREATE POLICY tenant_isolation ON __SCHEMA__.di_fact_adjudication_event
    USING (client_id = current_setting('app.current_client_id', true))
    WITH CHECK (client_id = current_setting('app.current_client_id', true));

-- ------------------------------------------------------------------------------------------
-- Grants (explicit, per the 006 convention — no ALL TABLES, no default privileges). No UPDATE/
-- DELETE: the table is append-only by trigger, so the write path only ever needs INSERT.
-- ------------------------------------------------------------------------------------------
GRANT SELECT, INSERT ON __SCHEMA__.di_fact_adjudication_event TO di_app_rw;
GRANT USAGE, SELECT ON __SCHEMA__.di_fact_adjudication_event_id_seq TO di_app_rw;
