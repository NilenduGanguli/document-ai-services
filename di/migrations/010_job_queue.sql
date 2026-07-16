-- 010_job_queue.sql — durable multi-worker queue semantics on di_job, plus the doc_version
-- ingest_complete flag that closes the at-least-once noop-on-retry hole. Idempotent, pure DDL.
--
-- Why each piece exists:
--   di_job queue columns  — kind/payload/priority/attempts/run_after/lease_expires_at/locked_by
--                           let ANY worker claim, heartbeat, reclaim and dead-letter work.
--                           payload is never exposed via the API (di/jobs.py excludes it from
--                           the public Job model) — it carries internal blob URIs.
--   di_job_claim index    — partial index over only claimable rows: the hot claim scan never
--                           touches terminal rows regardless of table size.
--   di_job_lease index    — partial index for the reaper's expired-lease scan.
--   status CHECK           — NOT VALID: constrains new writes without scanning/locking existing
--                           rows at deploy time. Vocabulary gains 'dead' (poison pill) and
--                           'canceled' (ships with its producer, the cancel endpoint, in this
--                           same phase — no dead vocabulary sitting unused in the constraint).
--   fillfactor/autovacuum  — di_job is a high-churn table under this design (status flips +
--                           heartbeats); HOT updates need page slack, and default autovacuum
--                           thresholds are too coarse for this table's dead-tuple rate.
--   No new RLS policy      — di_job's worker_claim policy (TO di_worker, USING (true)) already
--                           shipped in 006_roles_and_grants.sql, before this queue existed, so
--                           the posture guard and any security review see the final RLS shape
--                           only once. See di/db.py's _ALLOWED_EXTRA_POLICIES.
--   doc_version.ingest_complete
--                          — at-least-once retry hits the content-hash noop shortcut
--                           (di/pipeline.py) if a worker crashes after create_version commits but
--                           before knodes/areps/merge finish, silently succeeding a job with an
--                           empty subtree. DEFAULT true so every pre-existing row (written by the
--                           old synchronous-then-write pipeline, which never left this window
--                           open) is correctly treated as complete with no backfill. New rows are
--                           inserted with ingest_complete=false and flipped true in the same
--                           transaction as the last pipeline write (di/store.py:mark_version_complete).
--
-- Deliberately NOT here: the doc_version (client_id, doc_id, version_no) uniqueness backstop.
-- The runner executes this whole file as one implicit transaction (di/db.py), so a
-- non-concurrent unique index on doc_version would hold an ACCESS EXCLUSIVE lock for the file's
-- full duration while replicas are still writing it. That index ships one release later, after
-- this file's create_version advisory-lock/retry code has been running
-- (011_doc_version_unique.sql), gated by tools/check_doc_version_dupes.py — the two-release
-- sequencing a bank-grade rollout needs.

ALTER TABLE __SCHEMA__.di_job
    ADD COLUMN IF NOT EXISTS kind             text        NOT NULL DEFAULT 'ingest',
    ADD COLUMN IF NOT EXISTS payload          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS priority         smallint    NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS attempts         int         NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts     int         NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS run_after        timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS locked_by        text;

CREATE INDEX IF NOT EXISTS di_job_claim
    ON __SCHEMA__.di_job (priority, run_after, created_at, id)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS di_job_lease
    ON __SCHEMA__.di_job (lease_expires_at)
    WHERE status = 'running';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'di_job_status_check'
          AND conrelid = to_regclass('__SCHEMA__.di_job')
    ) THEN
        ALTER TABLE __SCHEMA__.di_job ADD CONSTRAINT di_job_status_check
            CHECK (status IN ('queued','running','succeeded','failed','dead','canceled'))
            NOT VALID;
    END IF;
END$$;

ALTER TABLE __SCHEMA__.di_job SET (
    fillfactor = 70,
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_analyze_scale_factor = 0.02
);

ALTER TABLE __SCHEMA__.doc_version
    ADD COLUMN IF NOT EXISTS ingest_complete boolean NOT NULL DEFAULT true;
