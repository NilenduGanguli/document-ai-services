-- 005_hardening.sql — production hardening: auth, async jobs, blob storage, adjudication,
-- and the audit/idempotency columns the pipeline computes but never persisted. Idempotent.
--
-- Adds five tables (two GLOBAL, three tenant-scoped) and back-fills the column gaps on 002's
-- tables via ADD COLUMN IF NOT EXISTS — editing 002 would only ever affect a fresh database,
-- never an existing one.
--
-- Why each piece exists:
--   di_api_key            — caller identity + per-key client/scope allow-lists (GLOBAL: a key
--                           grants access ACROSS clients, so it cannot be tenant-filtered).
--   di_job                — async ingest jobs: status/stage, an event log, and an idempotency
--                           key so a retried submit returns the original job, not a duplicate.
--   di_blob               — DB-backed blob fallback for deployments without object storage.
--   di_fact_adjudication  — human-in-the-loop verdicts. Kept in a SEPARATE table (not columns on
--                           client_merged_fact) so a re-merge, which rewrites the merged row, can
--                           never silently discard a reviewer's decision.
--   di_migration_ledger   — filename + checksum of every applied migration, so a mutated
--                           already-applied file is detectable (GLOBAL: infra, not tenant data).
--
-- NOTE: this migration is pure DDL by design. 004 has already ENABLE+FORCE'd RLS by the time
-- this file runs, so any DML here would be silently filtered to zero rows under the non-superuser
-- role the app uses in production. Back-fills belong in application code that binds the tenant GUC.

-- ---------------------------------------------------------------------------------------------
-- New tables
-- ---------------------------------------------------------------------------------------------

-- API keys. GLOBAL (no client_id, no RLS policy): a single key may be scoped to several clients
-- via client_ids, so it is looked up BEFORE any tenant GUC is bound. Only the hash is stored.
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_api_key (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash      text NOT NULL UNIQUE,
    name          text NOT NULL,
    client_ids    text[] NOT NULL DEFAULT '{}',
    scopes        text[] NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz,
    disabled_at   timestamptz
);

-- Async ingest jobs: one row per submitted document, carrying its own append-only event log.
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_job (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id        text NOT NULL,
    status           text NOT NULL DEFAULT 'queued',
    stage            text,
    document_name    text,
    doc_id           uuid,
    version_id       uuid,
    error            text,
    idempotency_key  text,
    events           jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);
-- Partial: many jobs are submitted without an idempotency key, and NULLs must not collide.
CREATE UNIQUE INDEX IF NOT EXISTS di_job_client_idem
    ON __SCHEMA__.di_job (client_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
-- Keyset pagination cursor: (created_at, id) DESC is a total order, so no OFFSET drift.
CREATE INDEX IF NOT EXISTS di_job_client_created
    ON __SCHEMA__.di_job (client_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS di_job_client_status ON __SCHEMA__.di_job (client_id, status);

-- Blob storage fallback: original bytes when no object store (S3/GCS/Azure) is configured.
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_blob (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id     text NOT NULL,
    key           text NOT NULL,
    content_type  text,
    size          int NOT NULL DEFAULT 0,
    sha256        text,
    data          bytea NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (client_id, key)
);

-- Human adjudication of a merged fact. Survives re-merge: the merger re-derives
-- client_merged_fact from source facts, then re-applies any verdict found here.
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_fact_adjudication (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id      text NOT NULL,
    attribute_key  text NOT NULL,
    verdict        text NOT NULL CHECK (verdict IN ('accept', 'reject', 'override')),
    value_text     text,
    value_date     date,
    value_num      double precision,
    reviewer       text,
    note           text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (client_id, attribute_key)
);

-- Applied-migration ledger. GLOBAL: infrastructure state, not tenant data, so no RLS.
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_migration_ledger (
    filename    text PRIMARY KEY,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- Column additions to existing (002) tables
-- ---------------------------------------------------------------------------------------------

-- The gate computes a rationale and anchor summary per document but persisted neither, leaving
-- di_decision_trace unable to answer "why was this document gated?" — a compliance gap.
ALTER TABLE __SCHEMA__.di_decision_trace
    ADD COLUMN IF NOT EXISTS rationale       text,
    ADD COLUMN IF NOT EXISTS anchor_summary  jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Merge outcome provenance: which fact won, under which ontology, and whether a human touched it.
ALTER TABLE __SCHEMA__.client_merged_fact
    ADD COLUMN IF NOT EXISTS verification_status   text,
    ADD COLUMN IF NOT EXISTS winning_fact_id       uuid,
    ADD COLUMN IF NOT EXISTS resolution_rationale  jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS ontology_version      text,
    ADD COLUMN IF NOT EXISTS adjudicated           boolean NOT NULL DEFAULT false;

-- Caller-supplied document id (for upsert-by-external-id) + where the original bytes live.
ALTER TABLE __SCHEMA__.di_documents
    ADD COLUMN IF NOT EXISTS external_document_id  text,
    ADD COLUMN IF NOT EXISTS blob_uri              text,
    ADD COLUMN IF NOT EXISTS blob_backend          text;

-- Partial: only documents that actually carry an external id are constrained.
CREATE UNIQUE INDEX IF NOT EXISTS di_documents_client_extid
    ON __SCHEMA__.di_documents (client_id, external_document_id)
    WHERE external_document_id IS NOT NULL;

-- Monotonic change-feed cursor. A shared (non-per-client) sequence is deliberate: it is an
-- opaque, gap-tolerant cursor, and consumers read it behind the client_id RLS filter anyway.
ALTER TABLE __SCHEMA__.doc_version
    ADD COLUMN IF NOT EXISTS change_seq bigint;

CREATE SEQUENCE IF NOT EXISTS __SCHEMA__.doc_version_change_seq;

-- Applied after the column + sequence exist; re-running simply re-asserts the same default.
ALTER TABLE __SCHEMA__.doc_version
    ALTER COLUMN change_seq SET DEFAULT nextval('__SCHEMA__.doc_version_change_seq');

-- Tie the sequence's lifetime to the column it feeds.
ALTER SEQUENCE __SCHEMA__.doc_version_change_seq
    OWNED BY __SCHEMA__.doc_version.change_seq;

-- Named for the columns, not the sequence: indexes and sequences share pg_class, so reusing
-- `doc_version_change_seq` here would collide with the sequence created above.
CREATE INDEX IF NOT EXISTS doc_version_client_change_seq
    ON __SCHEMA__.doc_version (client_id, change_seq);

-- ---------------------------------------------------------------------------------------------
-- RLS for the new tenant-scoped tables (same pattern as 004_rls.sql)
-- ---------------------------------------------------------------------------------------------
-- di_api_key and di_migration_ledger are intentionally absent: both are global and are read
-- before/outside any tenant context, so an `app.current_client_id` filter would return nothing.

DO $$
DECLARE
    t text;
    tables text[] := ARRAY[
        'di_job', 'di_blob', 'di_fact_adjudication'
    ];
BEGIN
    FOREACH t IN ARRAY tables LOOP
        EXECUTE format('ALTER TABLE __SCHEMA__.%I ENABLE ROW LEVEL SECURITY;', t);
        EXECUTE format('ALTER TABLE __SCHEMA__.%I FORCE ROW LEVEL SECURITY;', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON __SCHEMA__.%I;', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON __SCHEMA__.%I '
            'USING (client_id = current_setting(''app.current_client_id'', true)) '
            'WITH CHECK (client_id = current_setting(''app.current_client_id'', true));',
            t
        );
    END LOOP;
END$$;
