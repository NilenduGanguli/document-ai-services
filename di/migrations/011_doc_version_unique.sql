-- 011_doc_version_unique.sql — the doc_version (client_id, doc_id, version_no) uniqueness
-- backstop deferred out of 010. Idempotent, pure DDL.
--
-- Why this ships one release after 010, not in it:
--   The migration runner executes each file as one asyncpg multi-statement transaction
--   (di/db.py::run_migrations), so building this unique constraint non-concurrently would hold
--   an ACCESS EXCLUSIVE lock on doc_version for as long as the build takes, and Postgres refuses
--   to build any index concurrently inside a transaction block at all — this runner structurally
--   cannot use that mode. The split also matters for correctness, not just lock duration: once
--   this constraint exists, any replica still running pre-010 store.create_version() (no
--   per-document advisory lock, no retry-on-conflict) would hard-fail concurrent same-doc
--   ingests with an unhandled UniqueViolationError instead of the graceful noop/resume/new-version
--   decision. 010 shipped the advisory-locked create_version() plus a UniqueViolationError
--   retry-once backstop (di/store.py) a release before this file, so by the time this constraint
--   lands, every writer already knows how to lose that race cleanly.
--
-- Fresh / low-traffic databases: this file applies directly, in well under a second — there is
-- no pre-existing concurrent-ingest history to have produced duplicates, and the table is small.
--
-- Large, long-lived environments: run tools/check_doc_version_dupes.py FIRST (report, or
-- --repair to renumber any duplicates found — the advisory lock added in 010 prevents new ones,
-- this only cleans up rows written before that lock existed), then follow the out-of-band
-- concurrent-build procedure in docs/ops/production-cutover-runbook.md before deploying the
-- release that carries this file — a lock-duration hazard on a large table is not something to
-- discover live. Once that concurrent build has completed out-of-band, the statement below
-- becomes a fast catalog-only no-op, so the migration ledger records it as applied with no
-- further locking and no manual ledger row required.

CREATE UNIQUE INDEX IF NOT EXISTS doc_version_client_doc_no
    ON __SCHEMA__.doc_version (client_id, doc_id, version_no);
