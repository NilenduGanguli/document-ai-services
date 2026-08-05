-- 012_doc_version_blob.sql — record WHERE a version's source bytes live, on the version row
-- itself. Idempotent, pure DDL.
--
-- What already existed: 005 added blob_uri/blob_backend to di_documents, and store.insert_document
-- writes them on every ingest. But di_documents is UPSERTed per logical document (the conflict
-- target is (client_id, document_name) or (client_id, external_document_id)), so that pointer
-- always describes the CURRENT bytes — the locator of every superseded version is overwritten the
-- moment the next version lands. doc_version is the immutable chain (one row per distinct
-- content_hash) and recorded WHAT the bytes hashed to but never WHERE they are.
--
-- The objects themselves survive that overwrite (keys are content-addressed —
-- `{sha256}/{filename}` under a store-applied tenant prefix — and BLOB_RETAIN_AFTER_INGEST
-- defaults true), so this closes a bookkeeping gap, not a data-loss one. It matters because
-- "produce the exact bytes that were ingested as version 3" was a re-derivation from the key
-- scheme plus a backend guess, rather than a lookup — and that is precisely the question an audit,
-- a re-OCR backfill, or a dispute asks. It also makes the version-level story true for every
-- backend at once: `s3://bucket/key`, `pg://client/key`, `file:///path`.
--
-- Nullable and deliberately back-fill-free:
--   * versions written before this migration keep NULL — their bytes remain addressable via
--     di_documents.blob_uri (for the current one) or by re-deriving the content-addressed key;
--   * BLOB_BACKEND=none legitimately has no URI to record;
--   * a back-fill would be DML, and 004 has already FORCE'd RLS by the time migrations run, so it
--     would be silently filtered to zero rows under the non-superuser role production uses (the
--     same reason 005 is pure DDL). The pointer starts being recorded from here forward.
--
-- No index: this column is reached by version id / doc_id (both already indexed), never searched.

ALTER TABLE __SCHEMA__.doc_version
    ADD COLUMN IF NOT EXISTS blob_uri      text,
    ADD COLUMN IF NOT EXISTS blob_backend  text;
