-- 002_core_tables.sql — document metadata, versioning, entities, merged facts, audit trace.
-- Non-partitioned (lower volume than knode/arep); isolated by client_id + RLS (004). Idempotent.

-- One row per source file ingested for a client.
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_documents (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id           text NOT NULL,
    document_name       text NOT NULL,
    s3_uri              text,
    sha256              text,
    mime                text,
    doc_type            text,
    doc_category        text,
    subject             text,
    jurisdiction        text,
    lang_profile        jsonb NOT NULL DEFAULT '{}'::jsonb,
    sensitivity_bucket  text NOT NULL DEFAULT 'LOW',
    gate_decision       text,
    confidence          real NOT NULL DEFAULT 0,
    ocr_engine          text,
    page_count          int,
    ocr_text            text,
    ocr_lines           jsonb NOT NULL DEFAULT '[]'::jsonb,
    classification_signals jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,
    UNIQUE (client_id, document_name)
);
CREATE INDEX IF NOT EXISTS di_documents_client       ON __SCHEMA__.di_documents (client_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS di_documents_client_type  ON __SCHEMA__.di_documents (client_id, doc_type) WHERE deleted_at IS NULL;

-- Immutable version chain per logical document.
CREATE TABLE IF NOT EXISTS __SCHEMA__.doc_version (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       text NOT NULL,
    doc_id          uuid NOT NULL REFERENCES __SCHEMA__.di_documents(id) ON DELETE CASCADE,
    version_no      int NOT NULL,
    content_hash    text NOT NULL,
    supersedes      uuid,
    is_current      boolean NOT NULL DEFAULT true,
    changed_fields  jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text
);
CREATE UNIQUE INDEX IF NOT EXISTS doc_version_one_current
    ON __SCHEMA__.doc_version (client_id, doc_id) WHERE is_current;
CREATE INDEX IF NOT EXISTS doc_version_doc ON __SCHEMA__.doc_version (client_id, doc_id);

-- Entities referenced by knode.entity_ids (people / orgs / addresses within a client).
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_entity (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id        text NOT NULL,
    entity_type      text NOT NULL,
    normalized_name  text,
    attributes       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS di_entity_client ON __SCHEMA__.di_entity (client_id, entity_type);

-- Client-level merged knowledge view (intra-client consolidation; confidence-weighted).
CREATE TABLE IF NOT EXISTS __SCHEMA__.client_merged_fact (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id        text NOT NULL,
    attribute_key    text NOT NULL,
    resolved_value   text,
    value_date       date,
    value_num        double precision,
    confidence       real NOT NULL DEFAULT 0,
    conflict         boolean NOT NULL DEFAULT false,
    needs_review     boolean NOT NULL DEFAULT false,
    source_fact_ids  uuid[] NOT NULL DEFAULT '{}',
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (client_id, attribute_key)
);
CREATE INDEX IF NOT EXISTS client_merged_fact_client ON __SCHEMA__.client_merged_fact (client_id);

-- Per-document gate decision audit (compliance).
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_decision_trace (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id        text NOT NULL,
    doc_id           uuid,
    classification   jsonb NOT NULL DEFAULT '{}'::jsonb,
    pii_entities     jsonb NOT NULL DEFAULT '[]'::jsonb,
    sensitivity      text,
    gate_decision    text,
    lang_profile     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS di_decision_trace_client ON __SCHEMA__.di_decision_trace (client_id, created_at DESC);
