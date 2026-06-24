-- 003_knode_arep.sql — the knowledge subtree: knode (returned) + arep (searched).
-- HASH-partitioned by client_id (partitions created programmatically by di/db.py). Indexes on
-- the partitioned parents propagate to all current + future partitions. The pgvector
-- `content_embedding` / `rep_embedding` columns + per-partition HNSW indexes are added at runtime
-- by di/db.py (skipped when pgvector is absent). Idempotent.

-- knode: canonical logical & content nodes returned to consumers.
CREATE TABLE IF NOT EXISTS __SCHEMA__.knode (
    id                  uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id           text NOT NULL,
    doc_id              uuid NOT NULL,
    version_id          uuid NOT NULL,
    parent_id           uuid,
    path                ltree NOT NULL,
    node_type           text NOT NULL,
    seq                 int NOT NULL DEFAULT 0,
    depth               int NOT NULL DEFAULT 0,
    title               text,
    content             text,
    content_tsv         tsvector GENERATED ALWAYS AS
                          (to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(content,''))) STORED,
    context_prefix      text,
    attribute_key       text,
    value_text          text,
    value_date          date,
    value_num           double precision,
    verification_status text NOT NULL DEFAULT 'unverified',
    confidence          real NOT NULL DEFAULT 0,
    sensitivity         text NOT NULL DEFAULT 'LOW',
    valid_from          date,
    valid_to            date,
    cross_refs          uuid[] NOT NULL DEFAULT '{}',
    entity_ids          uuid[] NOT NULL DEFAULT '{}',
    provenance          jsonb NOT NULL DEFAULT '{}'::jsonb,
    token_count         int,
    created_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,
    PRIMARY KEY (client_id, id)
) PARTITION BY HASH (client_id);

CREATE INDEX IF NOT EXISTS knode_path_gist      ON __SCHEMA__.knode USING gist (path);
CREATE INDEX IF NOT EXISTS knode_tsv_gin        ON __SCHEMA__.knode USING gin (content_tsv);
CREATE INDEX IF NOT EXISTS knode_client_path    ON __SCHEMA__.knode (client_id, path);
CREATE INDEX IF NOT EXISTS knode_client_doc_ver ON __SCHEMA__.knode (client_id, doc_id, version_id);
CREATE INDEX IF NOT EXISTS knode_client_type    ON __SCHEMA__.knode (client_id, node_type);
CREATE INDEX IF NOT EXISTS knode_attr           ON __SCHEMA__.knode (client_id, attribute_key) WHERE node_type = 'fact';

-- arep: accessibility representations — searched, mapped back to their knode_id.
CREATE TABLE IF NOT EXISTS __SCHEMA__.arep (
    id           uuid NOT NULL DEFAULT gen_random_uuid(),
    knode_id     uuid NOT NULL,
    client_id    text NOT NULL,
    doc_id       uuid NOT NULL,
    version_id   uuid NOT NULL,
    path         ltree NOT NULL,
    rep_type     text NOT NULL,
    rep_lang     text NOT NULL DEFAULT 'en',
    rep_text     text NOT NULL,
    rep_tsv      tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(rep_text,''))) STORED,
    gen_model    text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, id)
) PARTITION BY HASH (client_id);

CREATE INDEX IF NOT EXISTS arep_path_gist    ON __SCHEMA__.arep USING gist (path);
CREATE INDEX IF NOT EXISTS arep_tsv_gin      ON __SCHEMA__.arep USING gin (rep_tsv);
CREATE INDEX IF NOT EXISTS arep_client_knode ON __SCHEMA__.arep (client_id, knode_id);
CREATE INDEX IF NOT EXISTS arep_client_type  ON __SCHEMA__.arep (client_id, rep_type);
