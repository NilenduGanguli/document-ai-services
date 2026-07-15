# Enterprise Scale-Out Plan — Document Intelligence

> Design spec for the four gaps called out at the end of the hardening release (commit 30c133d):
> in-process ingest runner, single-winner facts, RLS off in the demo, and demo-grade auth posture.
>
> Method: one designer per gap grounded in the code (every claim cites file:line), an adversarial
> staff-engineer review attacking each design (races under N replicas, crash windows, migration
> hazards, compliance gaps), and an integration pass resolving cross-design conflicts and fixing
> the build order. Reviewer corrections are part of the spec — implement the corrected design.
>
> Date: 2026-07-15 · Repo state: main @ 30c133d · Total plan effort: ~10–13 engineer-weeks

## Executive summary

| # | Upgrade | Decision | Effort |
|---|---|---|---|
| 1 | Ingest queue | **Postgres-native queue** on `di_job` (`FOR UPDATE SKIP LOCKED`, leases, attempts, dead-letter) + dedicated worker containers; blob persisted **at accept time**; broker deferred (di_job is already the outbox if ever needed) | L–XL |
| 2 | Multi-valued facts | Cardinality in the ontology + deterministic **value-fingerprint `instance_key`**; uniqueness `(client_id, attribute_key, instance_key)` with `''` sentinel; two-release cutover behind a flag | L |
| 3 | RLS production | **Role split**: `di_owner` (migrations) / `di_app_rw` / `di_worker` group roles, runtime `NOBYPASSRLS`; demo runs RLS **on**; fail-closed posture guard at boot | M |
| 4 | Auth | Key expiry+rotation, unified per-tenant admission quota (shared with the queue), append-only access audit (partitioned), `_assert_production_posture()` that crashes bad prod boots | L |

Build order (dependencies resolved by the integration pass): **0 foundations → 1 RLS → 2 auth → 3 multi-valued facts → 4 queue → 5 doc_version uniqueness + cutover.**
RLS goes first because every later migration creates objects whose grant/policy conventions it defines;
facts precede the queue because the queue multiplies same-client parallelism and needs the per-client
advisory lock + full-set replace semantics to already exist.

---

## 1. Durable ingest queue & horizontally scalable workers

**Effort:** L · **Reviewer verdict:** needs_changes (corrections below are normative)

### Current gap
Ingest execution is process-local and non-durable. POST /api/v1/ingest creates a di_job row, then schedules the pipeline with asyncio.create_task in the SAME process (di/ingest_runner.py:43-49), bounded only by a per-instance semaphore (di/ingest_runner.py:32-36, di/config.py:60). Consequences at N replicas: (1) a job only ever runs on the replica that accepted it — no work sharing, no failover; (2) if that process crashes, the task set is lost and the di_job row is stuck in 'queued'/'running' forever — nothing reclaims it (JobStatus has no retry/dead states, di/jobs.py:57-61; di_job has no attempts/lease columns, di/migrations/005_hardening.sql:42-56); (3) the raw file bytes exist ONLY in process memory between the 202 and the blob write, which happens inside the pipeline AFTER OCR (di/pipeline.py:197-198) and is explicitly non-fatal on failure (di/pipeline.py:146-148) — a crash between 202 and blob-write, or a blob-store outage, permanently loses the payload the bank just acknowledged accepting; (4) doc_version has no UNIQUE(client_id, doc_id, version_no) (di/migrations/002_core_tables.sql:34-48 — only a partial unique on is_current at :46-47), and version_no is computed from a read outside the insert transaction (di/pipeline.py:172-179, di/store.py:166-187), so two replicas ingesting different content for the same document concurrently write duplicate version numbers; (5) no queue-depth/age observability — only a per-process di_jobs_inflight gauge (di/observability.py:245-249); (6) no per-tenant fairness or backpressure — one tenant's backfill saturates the accepting instance's semaphore; (7) arep_async=true defers accessibility-rep work that nothing ever executes (di/pipeline.py:267-279) — deferred work is silently dropped.

### Options considered

**(a) Postgres-native queue on di_job (SELECT ... FOR UPDATE SKIP LOCKED) + dedicated worker deployment** — Extend di_job with lease/attempts/payload/kind columns; workers (separate deployment, same image) claim due jobs with FOR UPDATE SKIP LOCKED, heartbeat a lease, and a reaper requeues expired leases with exponential backoff until max_attempts, then dead-letters ('dead' status). File bytes are persisted to the blob store at ACCEPT time, before the job row is created; the job payload carries the blob URI. The API process can optionally embed the same consumer for the single-node compose demo.

- ✅ Zero new infrastructure: the job store, the queue, and the pipeline's writes share one transactional, backed-up, RLS-governed Postgres — the exact property a bank auditor wants (enqueue is atomic with job-row creation; no dual-write/outbox problem by construction)
- ✅ SKIP LOCKED work-claiming is boring, 10+-year-proven Postgres; the existing di_job table, idempotency index (005_hardening.sql:58-59), and keyset-paginated API (di/jobs.py:367-410) are already the right shape — this is an ALTER TABLE, not a re-platform
- ✅ Throughput ceiling is far above the workload: ingest is OCR-bound (seconds-to-minutes/doc, di/pipeline.py:187-195); even 100–200 claims/sec is trivial for SKIP LOCKED with a partial index, and millions of clients ≠ millions of jobs/sec
- ✅ Fairness, priorities, delayed retry, and dead-lettering are plain SQL — inspectable with psql during an incident, replayable, and covered by existing pg backup/PITR
- ✅ Maps cleanly to k8s (worker Deployment + HPA/KEDA postgres scaler) and Cloud Run (always-allocated worker service), and the compose demo just adds one service from the same image
- ❌ Queue churn creates dead tuples on di_job — needs fillfactor + autovacuum tuning and monitoring at high sustained rates
- ❌ Claiming requires cross-tenant visibility of di_job, which needs a deliberate, documented widening of the FORCE-RLS policy (005_hardening.sql:156-174) for the worker code path
- ❌ Polling adds up to poll-interval latency (mitigated with LISTEN/NOTIFY); no broker-native features like per-queue rate limits — fairness must be written as SQL
- ❌ DB becomes the single point of coupling for both serving and queueing (already true for job state today; sizing must account for worker connections)

**(b) External broker (Redis Streams / RabbitMQ / SQS) with di_job as outbox/state** — Accept path writes di_job + blob, then publishes a message to a broker; workers consume via consumer groups / competing consumers; di_job remains the source of truth for status, with an outbox relay or transactional publish pattern to keep DB and broker consistent.

- ✅ Purpose-built delivery semantics: visibility timeouts, DLQs, consumer groups out of the box
- ✅ Higher headroom for very high message rates; queue traffic off the primary DB
- ✅ SQS/managed brokers shift ops burden to the cloud provider (if the bank permits it)
- ❌ Dual-write problem: di_job row and broker message cannot be committed atomically — requires an outbox table + relay process, i.e. you end up building the Postgres queue ANYWAY plus a broker on top
- ❌ New stateful infrastructure to procure, harden, patch, DR-test, and get through bank security review (Redis persistence caveats; RabbitMQ mirroring; SQS = data-path cloud dependency + per-message PII considerations even for pointers)
- ❌ Compose demo grows another service and failure domain; local dev and CI get slower and flakier
- ❌ At this workload's rate (OCR-bound, not message-rate-bound) the broker's throughput advantage is unused — pure cost, no benefit today

**(c) Keep in-process, leader-sharded (each replica claims a shard of clients/jobs)** — Keep asyncio-task execution inside API replicas but coordinate via leases/shard assignment (e.g. advisory locks or a shard table) so each replica polls di_job for its shard; no dedicated workers.

- ✅ Smallest diff from today's ingest_runner.py; no new deployment unit
- ✅ No cross-tenant queue-read policy needed if sharding is by client_id with the GUC bound per shard
- ❌ Couples ingest capacity to API capacity: OCR/CPU-heavy jobs (di/ingest_runner.py:6-8 says exactly this) degrade read latency on the same pods, and you cannot scale ingest and serving independently — disqualifying at 'millions of clients' scale
- ❌ Shard rebalancing on deploy/scale-in is the hard 20% of a distributed queue, hand-rolled; hot tenants pin to one replica (a 250k backfill lands on whichever replica owns that shard)
- ❌ Still needs every durability fix from (a) — blob-at-accept, leases, attempts, dead-letter — so it is option (a)'s complexity without its operational separation
- ❌ API pod restarts (routine on k8s/Cloud Run) constantly interrupt long OCR jobs

### Recommendation
Option (a): Postgres-native queue on di_job with a dedicated worker deployment, plus an embeddable consumer for single-node/demo mode. For a bank at multi-instance scale this wins on every axis that matters: (1) Compliance/auditability — job acceptance, payload pointer, every state transition, retries, and dead-letters live in one ACID store already covered by RLS, backups, PITR, and the existing migration-ledger discipline; there is no second system whose retention, encryption, and access controls must be separately certified. (2) Fail-closed correctness — enqueue is a single transaction with job-row creation, eliminating the dual-write window that an external broker necessarily introduces (an outbox pattern would recreate option (a) inside option (b)). (3) Right-sized — the bottleneck is OCR (~seconds-to-minutes per document), not message rate; SKIP LOCKED handles orders of magnitude more claim throughput than this workload will generate even at millions of clients, and if the platform ever outgrows it, di_job-as-outbox means option (b) is a straightforward later evolution, not a rewrite. (4) Boring and operable — psql is the queue inspector; no new HA story, no new security review. (5) The demo path survives trivially: the same consumer code runs embedded in the API process for docker compose single-node mode, and compose also gains a real worker container so the production topology is exercised locally. Option (c) is disqualified because it cannot scale ingest independently of serving and hand-rolls shard rebalancing — the genuinely hard part — while still needing all of (a)'s durability work.

### Design (as proposed)
## Design: durable Postgres job queue + dedicated ingest workers

### 0. Principles
- **At-least-once execution, idempotent effects.** Exactly-once is not attainable across OCR/LLM side effects; instead every pipeline write is made idempotent (content-hash noop di/pipeline.py:171-185, di_documents upsert di/store.py:111-151, merged-fact upsert, plus the new doc_version unique index) and terminal job transitions are fenced by `locked_by` so a superseded worker cannot overwrite a reclaimed job's outcome.
- **Payload durable before acknowledgement.** The 202 is only returned after the raw bytes are in the blob store and the job row is committed. This inverts today's ordering (blob write after OCR, di/pipeline.py:197-198).
- **One consumer implementation, two deployment shapes.** `di/worker.py` runs either as a dedicated process (`python -m di.worker`) or embedded in the API lifespan (compose/demo). The old asyncio-task runner (di/ingest_runner.py) is deleted; there is no second execution path to drift.

### 1. Migration — `di/migrations/006_job_queue.sql`
Follows the house conventions: `__SCHEMA__` token (rewritten in di/db.py:212), idempotent DDL, **pure DDL only** (005_hardening.sql:20-22 explains why DML is forbidden under FORCE RLS), applied under the advisory lock + checksum ledger (di/db.py:186-233).

```sql
-- 006_job_queue.sql — durable multi-worker queue semantics on di_job, blob-at-accept payloads,
-- and the doc_version uniqueness backstop for concurrent ingest. Idempotent, pure DDL.
--
-- Why each piece exists:
--   di_job queue columns  — lease/attempts/backoff state so ANY worker can claim, heartbeat,
--                           reclaim and dead-letter work; payload carries the blob pointer so
--                           bytes never ride in process memory between accept and execution.
--   di_job_claim index    — partial index over only claimable rows: the hot claim scan never
--                           touches terminal rows regardless of table size.
--   queue_worker policy   — workers must see queued jobs ACROSS tenants before any tenant GUC
--                           can be bound; scoped to an explicit worker GUC, same trust model as
--                           app.current_client_id (di/db.py:91-92). Tenant work after claim is
--                           still done under the job's own client_id GUC.
--   doc_version unique    — two replicas ingesting different content for one doc both computed
--                           version_no = current+1 from a stale read; this makes the race a
--                           retryable conflict instead of silent duplicate version numbers.

ALTER TABLE __SCHEMA__.di_job
    ADD COLUMN IF NOT EXISTS kind             text        NOT NULL DEFAULT 'ingest',
    ADD COLUMN IF NOT EXISTS payload          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS priority         smallint    NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS attempts         int         NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts     int         NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS run_after        timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS locked_by        text;

-- Claim scan: only queued, due jobs. Partial => stays tiny however large di_job grows.
CREATE INDEX IF NOT EXISTS di_job_claim
    ON __SCHEMA__.di_job (priority, run_after, created_at, id)
    WHERE status = 'queued';

-- Reaper scan: running jobs whose lease may have lapsed.
CREATE INDEX IF NOT EXISTS di_job_lease
    ON __SCHEMA__.di_job (lease_expires_at)
    WHERE status = 'running';
-- (Per-tenant fairness counting reuses di_job_client_status from 005_hardening.sql:63.)

-- Status vocabulary now includes 'dead' (poison pill, needs operator action) and 'canceled'.
-- NOT VALID: constrain new writes without scanning/locking existing rows at deploy time.
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

-- Reduce dead-tuple pressure from high-churn status updates (HOT updates stay on-page).
ALTER TABLE __SCHEMA__.di_job SET (
    fillfactor = 70,
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_analyze_scale_factor = 0.02
);

-- Workers claim before any tenant context exists; policies are permissive (OR-combined) so
-- this coexists with tenant_isolation from 005. The GUC is set ONLY by di.db.acquire_worker().
DROP POLICY IF EXISTS queue_worker_access ON __SCHEMA__.di_job;
CREATE POLICY queue_worker_access ON __SCHEMA__.di_job
    USING (current_setting('app.job_queue_worker', true) = 'on')
    WITH CHECK (current_setting('app.job_queue_worker', true) = 'on');

-- Backstop for the concurrent-version race (see di/store.py create_version).
CREATE UNIQUE INDEX IF NOT EXISTS doc_version_client_doc_no
    ON __SCHEMA__.doc_version (client_id, doc_id, version_no);
```
**Pre-deploy check** (because a unique index cannot be created over duplicates): ship `tools/check_doc_version_dupes.py` that reports/repairs `(client_id, doc_id, version_no)` duplicates; run it before rolling this out to any long-lived environment. Fresh compose databases are unaffected.

### 2. Accept path — `di/routers/ingest.py`
Rework the non-stream branch of `ingest()` (currently di/routers/ingest.py:78-92):
1. Read + size-check upload (unchanged, :36-48).
2. `content_hash = versioning.content_hash(content)`.
3. **Backpressure (fail-closed, explicit):** cheap counts via the `di_job_client_status` index — if tenant queued ≥ `ingest_tenant_max_queued` or global queued ≥ `ingest_global_max_queued` → `429` with `Retry-After` (new error contract, documented; this is how a 250k backfill is throttled at the door instead of starving the fleet).
4. **Blob first:** `ref = await get_blob_store().put(client_id=..., key=blob_key(client_id, content_hash, filename), data=content, content_type=mime)` (di/storage/__init__.py:94-110, base.py:94-129 — keys are content-addressed, so retries upsert the same object). **Failure here is fatal: 503, no job row.** No 202 is ever issued for bytes we cannot durably produce later. If `blob_backend == "none"` (di/config.py:85), the 202 path returns 503 with a clear message at request time AND readiness reports the misconfiguration at startup; `?stream=true` (inline SSE, bytes stay in request memory, di/routers/ingest.py:68-76) remains available.
5. `jobs.enqueue(...)` — one INSERT committing status='queued' **and** `payload = {"blob_uri": ref.uri, "blob_backend": ref.backend, "content_hash": ..., "filename": ..., "mime": ..., "external_document_id": ..., "created_by": principal.name, "size": len(content)}` atomically. Existing idempotency behavior preserved: pre-check (:78-83) + unique-violation recovery (di/jobs.py:230-236) on `di_job_client_idem` (005_hardening.sql:58-59).
6. `NOTIFY di_job_new` (same connection, post-insert) — wake latency optimization; correctness never depends on it.
7. Return the same `IngestAccepted` 202 body (API contract unchanged; `Job` JSON gains additive fields `kind`, `attempts`, `max_attempts`, `run_after`).
Crash between blob-put and job-insert leaves only an orphaned content-addressed blob — harmless, idempotently overwritten on client retry; an optional weekly GC tool can sweep unreferenced blobs.

### 3. Queue operations — `di/jobs.py` (extended)
- `JobStatus` gains `dead`, `canceled` (di/jobs.py:57-61); `_TERMINAL` gains `dead`, `canceled` (:65). `Job` model + `_JOB_COLS` (:48-51) gain the new columns.
- New `di/db.py` helper `acquire_worker()` — mirrors `acquire()` (di/db.py:82-98) but sets `app.job_queue_worker='on'` for the checkout and resets it on release. This is the ONLY code path that sets the worker GUC.
- `enqueue(*, client_id, kind, payload, document_name, idempotency_key, priority, max_attempts) -> Job` — replaces the create-then-submit split.
- `claim(worker_id: str, *, batch: int, kinds: tuple[str, ...]) -> list[Job]` — two-step fair claim under `acquire_worker()`:
  ```sql
  WITH ranked AS (
    SELECT id, row_number() OVER (PARTITION BY client_id
                                  ORDER BY priority, created_at, id) AS rn
    FROM di_job
    WHERE status = 'queued' AND run_after <= now() AND kind = ANY($kinds)
      AND client_id NOT IN (        -- per-tenant running cap across the whole fleet
        SELECT client_id FROM di_job WHERE status = 'running' AND kind = ANY($kinds)
        GROUP BY client_id HAVING count(*) >= $tenant_max_running)
    ORDER BY rn, priority, created_at, id     -- round-robin across tenants first
    LIMIT $candidate_window)
  UPDATE di_job j SET status='running', locked_by=$worker_id, attempts=attempts+1,
         lease_expires_at = now() + $lease, stage = COALESCE(stage,'claimed'), updated_at = now()
  FROM (SELECT id FROM di_job WHERE id IN (SELECT id FROM ranked)
        AND status='queued' FOR UPDATE SKIP LOCKED LIMIT $batch) c
  WHERE j.id = c.id
  RETURNING <job cols>;
  ```
  (Window functions can't combine with FOR UPDATE in one level; the re-check `status='queued'` + SKIP LOCKED in the locking subquery makes lost races shrink the batch harmlessly.) The `rn`-first ordering interleaves tenants — tenant B's single job is claimed in the first pass even if tenant A has 250k queued — and the running-cap subquery bounds any tenant's share of fleet concurrency.
- `heartbeat(worker_id, job_ids)` — `UPDATE ... SET lease_expires_at = now()+$lease WHERE id = ANY(...) AND locked_by=$worker_id AND status='running'`; rows returned < requested ⇒ that job was reclaimed: the worker cancels its local task.
- `complete(worker_id, job_id, status, ...)` — terminal transition **fenced**: `WHERE id=$id AND locked_by=$worker_id AND status='running'`; 0 rows ⇒ log-and-discard (a reclaimed attempt finished elsewhere).
- `release(worker_id, job_ids)` — voluntary requeue on graceful shutdown: back to `queued`, `run_after=now()`, `attempts = attempts - 1` (a clean drain is not a failure), clear lock fields.
- `reap(*, backoff_base, backoff_cap) -> (requeued, dead)` — run by every worker each `reaper_interval` with jitter:
  ```sql
  -- retryable expiry
  UPDATE di_job SET status='queued', locked_by=NULL, lease_expires_at=NULL,
         run_after = now() + least($cap, $base * 2^attempts) * (0.5 + random()),
         updated_at = now()
  WHERE status='running' AND attempts < max_attempts
    AND (lease_expires_at < now()
         OR (lease_expires_at IS NULL AND updated_at < now() - interval '15 minutes'));
  -- poison pills
  UPDATE di_job SET status='dead', error = coalesce(error,'lease expired; max attempts reached'),
         locked_by=NULL, lease_expires_at=NULL, finished_at=now(), updated_at=now()
  WHERE status='running' AND attempts >= max_attempts AND lease_expires_at < now();
  ```
  The `lease_expires_at IS NULL` clause also rescues pre-006 rows orphaned 'running'/'queued' by old-world crashes.
- `queue_stats()` — per (kind,status) counts + `min(created_at)` of queued, feeding metrics.

### 4. Worker — new `di/worker.py` (+ `python -m di.worker` entrypoint)
`Consumer` class owning: claim loop (LISTEN `di_job_new` with `poll_interval` fallback), an asyncio semaphore of `ingest_concurrency` (reuses di/config.py:60 semantics per worker process), a heartbeat task (`lease/4`), a jittered reaper task, and a metrics/health endpoint (`prometheus_client.start_http_server` on `worker_metrics_port` + `/health` for the container healthcheck). Dispatch by `kind`:
- `ingest`: `acquire(client_id)` binds the tenant GUC as today (di/db.py:91-92); fetch bytes with `store.get(payload["blob_uri"], client_id=...)`; drive `ingest_document(...)` exactly as di/ingest_runner.py:53-79 does today (append_event per stage, terminal complete()), passing the pre-persisted blob ref so `_retain_blob` is skipped (see §5). `BlobNotFound` ⇒ non-retryable ⇒ immediate `failed` with explicit error.
- `arep`: executes the deferred accessibility-rep generation that `arep_async=true` currently drops on the floor (di/pipeline.py:267-279): payload `{doc_id, version_id}`, loads content nodes, runs `arep_mod.generate_areps` + embed + insert. The ingest pipeline's arep stage enqueues this job instead of skipping.
Worker failure taxonomy: exception from the pipeline ⇒ `complete(..., failed)` with error recorded IF `attempts >= max_attempts`, else `release-with-backoff` (status='queued', run_after=backoff) so transient OCR/LLM/gateway failures retry automatically; a small `NonRetryableError` set (BlobNotFound, validation errors) short-circuits to `failed`. Graceful shutdown: SIGTERM ⇒ stop claiming ⇒ `asyncio.wait(in-flight, timeout=drain_timeout)` (pattern from di/ingest_runner.py:84-92) ⇒ `release()` whatever didn't finish ⇒ exit 0.

### 5. Pipeline changes — `di/pipeline.py`
- `ingest_document(...)` gains `blob: BlobRef | None = None` and `content_hash: str | None = None`; when provided (queue path), skip `_retain_blob` (di/pipeline.py:134-148, 197-198) and the hash recompute, and stamp `meta.blob_uri/blob_backend` from the ref (di/pipeline.py:233). The `stream=true` inline path passes neither and behaves exactly as today — no contract change.
- New config `blob_retain_after_ingest: bool = True`; when False the worker deletes the blob after a terminal `succeeded` (policy analogue of today's `blob_backend=none`: bytes must exist between accept and execution, retention afterwards is the operator's choice).
- `di/store.py:create_version` (di/store.py:166-187): take `pg_advisory_xact_lock(hashtextextended(client_id || ':' || doc_id, 0))` at the top of the transaction and re-read the current version inside it before deciding `version_no` (moving the `decide_version` call, currently at di/pipeline.py:175-179, behind the lock or re-validating under it); the new unique index is the backstop — on `UniqueViolationError`, re-read and retry once, degrading to noop if the winner wrote the same content_hash.

### 6. Runner replacement + single-node mode
- **Delete** `di/ingest_runner.py`; `di/routers/ingest.py:19,87-90` switches to `jobs.enqueue`. In-memory byte handoff no longer exists anywhere.
- `di/app.py` lifespan (di/app.py:142-153): when `settings.ingest_embedded_worker` is true, start a `di.worker.Consumer` as a lifespan-owned task; `_shutdown()` calls `consumer.drain()` instead of `ingest_runner.drain()` (di/app.py:143). Default `ingest_embedded_worker=True` ⇒ `pip install + uvicorn` single-process dev keeps working with zero extra processes; production sets it False and deploys dedicated workers. Startup readiness (di/app.py:75-139) adds a `queue` component: verifies the 006 columns exist and (fail-closed) reports not-ready when `blob_backend == "none"` while async ingest is enabled.

### 7. Config additions — `di/config.py`
```python
# --- Job queue / workers ---
ingest_embedded_worker: bool = True      # API process also consumes the queue (single-node/demo)
job_lease_seconds: int = 300             # > worst-case OCR stage; heartbeat renews at lease/4
job_max_attempts: int = 3
job_retry_base_seconds: float = 30.0
job_retry_max_seconds: float = 3600.0
job_poll_interval_seconds: float = 2.0   # fallback when NOTIFY is missed
job_claim_batch: int = 4
job_reaper_interval_seconds: float = 30.0
worker_metrics_port: int = 9090
ingest_tenant_max_running: int = 4       # fleet-wide per-tenant concurrency cap
ingest_tenant_max_queued: int = 50_000   # accept-side backpressure -> 429
ingest_global_max_queued: int = 500_000
blob_retain_after_ingest: bool = True
```

### 8. Observability — `di/observability.py`
New collectors alongside the existing set (di/observability.py:196-250): `di_queue_depth{kind,status}` gauge and `di_queue_oldest_age_seconds{kind}` gauge (refreshed from `queue_stats()` every 15s by each worker — cheap counts on partial indexes), `di_jobs_claimed_total{kind}`, `di_jobs_retried_total{kind}`, `di_jobs_dead_total{kind}` (page-worthy alert), `di_job_duration_seconds{kind}` histogram (claim→terminal), `di_worker_leases_lost_total`. Alerts: dead>0, oldest_age > SLO, depth sustained growth, leases_lost spike.

### 9. Deployment
docker-compose (docker-compose.yml): factor the app environment into a YAML anchor `x-di-env: &di-env`; app service adds `INGEST_EMBEDDED_WORKER: "false"`; add:
```yaml
  worker:
    build: .
    command: ["python", "-m", "di.worker"]
    environment:
      <<: *di-env
    volumes:
      - blobdata:/data/blobs
    depends_on:
      db: { condition: service_healthy }
      azure-ocr-mock: { condition: service_healthy }
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://localhost:9090/health')\""]
      interval: 5s
      timeout: 3s
      retries: 20
```
`docker compose up --scale worker=3` demonstrates horizontal claim distribution locally. Migrations still run once under the advisory lock (di/db.py:199-200) from whichever process boots first; the worker waits for the `queue` readiness of the schema (simple retry on missing columns). K8s later: API = Deployment behind the LB; worker = separate Deployment (no Service needed), HPA via KEDA `postgresql` scaler on `count(*) FROM di_job WHERE status='queued'`; SIGTERM grace ≥ drain timeout. Cloud Run: API as a normal service; worker as an instance-based (always-allocated CPU) service with min-instances ≥ 1, or Cloud Run Jobs for burst drains.

### 10. Failure modes handled
| Failure | Behavior |
|---|---|
| Crash after blob-put, before job insert | Orphan content-addressed blob; client retry (same idempotency_key) reuses/overwrites; optional GC sweep |
| Crash after 202 (today: payload lost) | Bytes durable in blob store; job claimed by any worker |
| Worker crash / OOM mid-job | Lease expires → reaper requeues with exponential backoff + jitter; attempts++ |
| Poison document (OCR segfaults worker repeatedly) | attempts ≥ max_attempts → status='dead', `di_jobs_dead_total` alert; operator `POST /api/v1/jobs/{id}/retry` (new admin-scoped endpoint in di/routers/jobs.py) after diagnosis |
| Zombie worker (paused VM resumes after reclaim) | Terminal `complete()` fenced on `locked_by` matches 0 rows → discarded; heartbeat mismatch cancels the local task early; DB writes were idempotent (upserts + unique indexes) |
| Duplicate execution effects | di_documents upsert (di/store.py:144), content-hash noop (di/pipeline.py:180-185), doc_version unique index + advisory-lock re-read, merged-fact UNIQUE (002:74); job `events` may contain duplicated stage entries across attempts — each JobEvent gains an `attempt` field so the timeline reads honestly |
| Tenant backfill floods queue | 429 at accept beyond per-tenant cap; claim-side round-robin + per-tenant running cap keeps other tenants' latency flat |
| DB down | Workers back off and retry connect; API 503s (readiness already truthful, di/app.py:80-84) |
| Missed NOTIFY | Poll interval floor guarantees progress |

### 11. Rollout
1. Land 006 + code; deploy API replicas (rolling; old pods drain their in-process asyncio jobs via existing drain, di/app.py:143). 2. Deploy workers. 3. Flip `INGEST_EMBEDDED_WORKER=false` on API. 4. Pre-006 stuck rows are rescued by the reaper's NULL-lease clause. Rollback: re-enable embedded worker and scale workers to 0 — schema changes are purely additive.

### 12. Test plan
- **Unit (no DB, matches house style):** backoff schedule math; claim-SQL builder; JobStatus terminal/fencing state machine; payload model round-trip; `blob_backend=none` × dispatch-mode validation.
- **Integration (`DI_RUN_INTEGRATION=1`):** enqueue→claim→complete happy path; two concurrent claimers never double-claim (spawn 8 claimers over 100 jobs, assert disjoint); lease expiry → reclaim → attempts++ → dead at max_attempts; fenced complete after reclaim is a no-op; voluntary release does not consume an attempt; fairness — 1k jobs tenant A + 1 job tenant B ⇒ B completes within one claim batch; per-tenant running cap honored; idempotency-key double-submit returns the same job under both old and new columns; concurrent same-doc different-content ingest ⇒ distinct version_no (unique index + advisory lock), same-content ⇒ noop; RLS on — worker GUC sees cross-tenant queue rows, tenant GUC does not see foreign jobs, no-GUC sees nothing (finally exercising 004/005 policies); blob-at-accept — kill worker between claim and OCR, assert bytes retrievable and job replays.
- **Chaos (compose-based script):** `docker kill` a worker mid-OCR ⇒ job completes elsewhere within lease+reaper interval; `--scale worker=3` throughput ≥ 1-worker baseline × ~2.5.
- **Smoke (tools/smoke_test.py):** extend the 47 checks: 202 → poll to succeeded via worker container; `/metrics` shows queue depth/age; dead-letter + retry endpoint round-trip; 429 backpressure with a lowered cap env.

### Code touchpoints
- `di/migrations/006_job_queue.sql (new)`
- `di/jobs.py:57-61 (JobStatus + dead/canceled), di/jobs.py:48-51 (_JOB_COLS), di/jobs.py:204-239 (create_job -> enqueue), new claim/heartbeat/complete/release/reap/queue_stats functions`
- `di/db.py:82-98 (new acquire_worker() sibling of acquire())`
- `di/worker.py (new: Consumer, claim loop, heartbeat, reaper, kind dispatch, __main__)`
- `di/ingest_runner.py (deleted; replaced by di/worker.py + jobs.enqueue)`
- `di/routers/ingest.py:19 (import), di/routers/ingest.py:78-92 (blob-first accept, backpressure 429, enqueue+NOTIFY)`
- `di/routers/jobs.py (new POST /api/v1/jobs/{id}/retry, admin/ingest scope)`
- `di/pipeline.py:151-159 (signature: blob ref + content_hash params), di/pipeline.py:171-185 (hash/version block), di/pipeline.py:197-198 (skip _retain_blob when pre-persisted), di/pipeline.py:267-279 (enqueue kind='arep' instead of dropping deferred work)`
- `di/store.py:166-187 (create_version: advisory xact lock + in-txn re-read + conflict retry)`
- `di/config.py:60 (ingest_concurrency reused per-worker) + new queue/worker/backpressure settings block`
- `di/app.py:142-153 (lifespan: embedded Consumer start/drain), di/app.py:75-139 (readiness: queue component, blob_backend=none fail-closed)`
- `di/observability.py:196-250 (queue depth/age/claimed/retried/dead/duration collectors)`
- `docker-compose.yml:28-79 (env anchor, INGEST_EMBEDDED_WORKER=false, new worker service)`
- `tools/smoke_test.py (queue-path checks), tools/check_doc_version_dupes.py (new pre-deploy check)`
- `tests/test_jobs.py + new tests/test_worker.py, tests/integration additions`

### Risks
- di_job becomes a high-churn table: status flips + heartbeats generate dead tuples; mitigated by fillfactor 70 / aggressive autovacuum / partial indexes, but sustained multi-hundred-jobs/sec would need heartbeat batching or a move to option (b) with di_job as outbox — monitor n_dead_tup and claim latency from day one
- The queue_worker_access RLS policy deliberately widens di_job visibility for connections that set the worker GUC; it uses the same set_config trust model as app.current_client_id (di/db.py:91-92), but a security reviewer may require a dedicated DB role instead — the policy can be re-pointed at a role (TO di_worker) without schema changes, and RLS remains untested in compose (RLS_ENABLED=false, docker-compose.yml:39) until the new integration tests land
- CREATE UNIQUE INDEX doc_version_client_doc_no fails at migration time if historical duplicate version_no rows exist in a long-lived environment — the pre-deploy dedupe check is mandatory, and because migrations run at app startup (di/app.py:104-109) a failure boots the app degraded (readiness=false) rather than crashed; ops must watch /readyz on first deploy
- At-least-once semantics mean external side effects (Azure OCR calls, LLM egress) can run twice for one document after a lease expiry; costs are bounded by max_attempts but di_llm_egress_total (di/observability.py:227-231) will count re-attempts — compliance reporting should be told the metric is attempt-scoped, not document-scoped
- Behavior change: blob_backend=none can no longer support async (202) ingest — deployments relying on it must either accept transient retention (blob_retain_after_ingest=false) or use stream=true; this is an explicit, documented contract narrowing
- The 429 backpressure response is a new client-visible behavior on POST /ingest; integrators must implement Retry-After handling before caps are set below their burst sizes
- Fairness claim query (window function + anti-join) is more expensive than a bare SKIP LOCKED scan; with very large queued backlogs (250k+) the candidate window keeps it bounded, but the query plan should be verified with a seeded backlog in the chaos test before production sizing

### Adversarial review — corrections (normative)

**Factual errors found in the proposal:**
- The global backpressure cap is claimed implementable via 'cheap counts via the di_job_client_status index' (005_hardening.sql:63) on the accept path — but the accept path binds the tenant GUC (di/db.py:91-92) and di_job is ENABLE+FORCE RLS with the tenant_isolation policy (005_hardening.sql:163-173), so a cross-tenant 'global queued' count returns only the caller's tenant rows. It only appears to work locally because compose sets RLS_ENABLED=false and connects as the POSTGRES_USER bootstrap superuser (docker-compose.yml:13,39), which bypasses FORCE RLS. As specified, the global cap contradicts the design's own claim that acquire_worker() is 'the ONLY code path that sets the worker GUC'.
- Rollout step 4: 'Pre-006 stuck rows are rescued by the reaper's NULL-lease clause' — pre-006 rows get payload '{}' from the ADD COLUMN default and their bytes existed only in the dead process's memory (di/ingest_runner.py:39-49 in-memory handoff; blob write only at di/pipeline.py:197-198), so they cannot be rescued — they will be claimed and must be explicitly failed (or crash the worker with a KeyError if unhandled). 'Rescued' is wrong as stated.
- Design Principle 0 asserts 'every pipeline write is made idempotent' citing the content-hash noop (di/pipeline.py:171-185) — verified false for the crash window between create_version (di/pipeline.py:242) and insert_knodes (di/pipeline.py:262): the noop check then converts a retried partial ingest into a 'succeeded' job with no knodes/areps/merge for that version. insert_knodes/insert_areps are also plain INSERTs with fresh UUIDs (di/store.py:206+), not idempotent upserts.
- Minor citation nits (verified, immaterial): meta.blob_uri/blob_backend stamping is di/pipeline.py:232, not :233; insert_document spans di/store.py:111-152, not 111-151. All other ~40 file:line citations in the design were checked and are accurate.

**Design flaws to fix:**
- Noop-shortcut vs partial-pipeline crash: create_version (di/pipeline.py:242) precedes insert_knodes (:262)/areps (:265-279)/merge (:282); a worker crash in that window plus at-least-once retry hits the content-hash noop (di/pipeline.py:174-185) and marks the job 'succeeded' with zero knodes for the version — silent data loss. The 'idempotent effects' principle is false here.
- Only terminal transitions are fenced on locked_by; append_event/set_status (di/jobs.py:242-302) are not. A zombie worker flips a requeued job back to 'running' with NULL locked_by/lease (invisible to claim), keeps bumping updated_at (defeating the 15-minute NULL-lease rescue indefinitely on long OCR jobs), and interleaves events across attempts.
- Backpressure runs before the idempotency pre-check: a retried submit of an already-accepted job can receive 429 under load, breaking the idempotency contract preserved at di/routers/ingest.py:78-83.
- Fairness is not actually enforced: the locking subquery (IN + FOR UPDATE SKIP LOCKED + LIMIT) has no ORDER BY, so Postgres may lock any rows from the candidate window; and the per-tenant running-cap anti-join is evaluated without locks, so concurrent claimers can exceed the cap — it is a soft cap presented as hard.
- Concurrent _remerge_client_facts across workers for the same tenant (up to tenant_max_running=4) is last-writer-wins over a stale fact snapshot — the merged view can drop the newer document's facts until the next ingest; no per-client serialization is specified, and the design leans on the client_merged_fact UNIQUE (002_core_tables.sql:74) that the multi-valued-facts upgrade will change.
- The create_version advisory-lock fix re-decides version_no inside the lock, but the ltree base path (di/pipeline.py:247), supersedes_id (:244) and the done event (:287) still use the stale pre-lock plan — knode paths and events can disagree with the stored version row.
- Migration hazard: the runner executes each file as one asyncpg multi-statement implicit transaction (di/db.py:214), holding ACCESS EXCLUSIVE locks on di_job and doc_version (non-concurrent CREATE UNIQUE INDEX) for the whole file while old replicas actively write both tables during the rolling deploy; CREATE INDEX CONCURRENTLY is impossible under this runner and the design does not say so.
- Rollout ordering hazard: once doc_version_client_doc_no exists, still-running old-code replicas have no UniqueViolation handling in create_version (di/store.py:179-186) and will hard-fail concurrent same-doc ingests mid-deploy; the design ships index and handling in the same release instead of code-before-index across two releases.
- Split terminal taxonomy: attempts exhausted via pipeline exception ends 'failed', via lease expiry ends 'dead' — the page-worthy di_jobs_dead_total alert misses every deterministically-failing poison document.
- Claimed-but-locally-queued jobs: job_claim_batch (4) equals ingest_concurrency (4) but nothing ties claims to free semaphore slots; claimed jobs waiting locally either burn lease unheartbeated (spurious reclaim -> double execution) or are heartbeated while hostage to a stuck worker — unspecified either way.
- Queue depth/age gauges are refreshed only by workers (di/observability.py pattern), so the depth metric goes stale precisely when all workers are down — the exact incident it exists to catch; the API process exports no queue gauges.
- LISTEN/NOTIFY is specified against the shared asyncpg pool, but the pool recycles idle connections after 300s (di/db.py:69); LISTEN requires a dedicated long-lived connection, unspecified.
- Compliance: 'optional weekly GC' for orphaned content-addressed PII blobs and no retention TTL for dead-job payload blobs is not bank-acceptable; tenant offboarding (purge_client_jobs, di/jobs.py:305-317) does not cover cancelling running work or purging job payload blobs.
- The Job model/_JOB_COLS 'gain the new columns' — if payload is included, internal blob URIs and filesystem paths leak to API callers via GET /api/v1/jobs; and new JobStatus values ('dead','canceled') in an existing response field break strict-enum clients despite the 'API contract unchanged' claim; 'canceled' is added with no cancel endpoint.
- ingest_embedded_worker defaults True: a production deployment that misses one env var silently runs OCR on API pods — recreating exactly the option (c) coupling the design disqualifies; the default should key off settings.is_production (di/config.py:122-123).
- The GUC-based queue_worker_access policy uses a trust model the design itself predicts the security review (and the known RLS role-split upgrade) will reject — shipping it guarantees a second migration and re-review; and RLS remains untested in compose (RLS_ENABLED=false, docker-compose.yml:39) where the bootstrap superuser bypasses FORCE RLS entirely, masking policy bugs until production.
- blob_backend=local silently breaks the multi-node topology (API writes to its node-local disk; workers elsewhere get BlobNotFound -> non-retryable failed) — compose shares a volume but the k8s/Cloud Run section never states that local requires a shared RWX volume or is disallowed.

**Missing pieces:**
- Explicit handling for empty-payload (pre-006) jobs in the worker: fail with a distinct 'payload lost in pre-durable-queue upgrade' error, excluded from poison-pill alerting.
- worker_id generation and uniqueness guarantees (pod name + PID + random suffix across restarts) — the entire locked_by fencing scheme depends on it and it is never specified.
- Idempotency for the new 'arep' job kind: a retried ingest attempt can enqueue duplicate arep jobs, and arep has no unique constraint (002/003), so re-execution duplicates accessibility reps — needs an idempotency key per (version_id) arep job plus an arep upsert or unique index.
- Cancel endpoint (or removal of the 'canceled' status from 006) — a status with no producer is dead vocabulary that still widens the CHECK constraint forever (migration files cannot be edited later).
- Documented 429/Retry-After error contract in OpenAPI plus integrator comms plan — the design puts it in smoke tests but not in the API contract surface.
- A statement of how the migration runner's single-transaction execution forbids CREATE INDEX CONCURRENTLY, and the out-of-band procedure (index concurrently + manual ledger entry) for large long-lived doc_version tables.
- Per-client remerge serialization (advisory lock or coalesced 'remerge' job kind) and an explicit compatibility note against the multi-valued-facts upgrade that changes client_merged_fact's UNIQUE (002_core_tables.sql:74).
- API-side export of queue_stats (or a staleness alert) so queue depth is observable when zero workers are alive.
- k8s/Cloud Run constraint that blob_backend=local requires a shared RWX volume across API and worker, or is disallowed for multi-node.
- Retention/TTL policy for dead-job payload blobs and extension of tenant offboarding (purge_client_jobs + BlobStore.delete_client) to job payload blobs and in-flight work — right-to-erasure currently misses queue payloads.
- Integration test for the noop-vs-partial-crash window (kill worker between create_version and insert_knodes, assert the retry rebuilds the subtree rather than reporting noop success) — the listed test plan covers claim/lease mechanics but not this, the most dangerous correctness case.

**Corrected design deltas:**
1. CLOSE THE NOOP-ON-RETRY HOLE (blocking). Pipeline order is create_version (di/pipeline.py:242) BEFORE insert_knodes (:262), areps (:265-279), and merge (:282). Under at-least-once retry, a worker crash after create_version commits but before insert_knodes makes the retry hit the content-hash noop shortcut (di/pipeline.py:174-185: hash matches the now-current version) and yield done/noop — the job terminates 'succeeded' with ZERO knodes, areps, and no re-merge for that version. Silent data loss disguised as success; the design's Principle 0 ("every pipeline write is idempotent") is false for this window. Fix: add an ingest_complete flag on doc_version set in the same transaction as the last pipeline write (or make version+knodes one transaction), and make the noop check require is_current AND ingest_complete; a retry of an incomplete version must DELETE that version's knodes/areps and rewrite them in one transaction (knode/arep inserts are plain INSERTs with fresh UUIDs, di/store.py:206+, so re-execution without cleanup duplicates rows).

2. FENCE ALL WORKER-SIDE JOB WRITES, not just terminal complete(). append_event and set_status (di/jobs.py:242-302) filter only on client_id+id. A zombie worker (design's own scenario) driving the pipeline "exactly as di/ingest_runner.py:53-79 does today" will: flip a reaper-requeued 'queued' job back to 'running' with locked_by=NULL and lease NULL (invisible to the claim scan), keep bumping updated_at every stage (indefinitely deferring the 15-minute NULL-lease rescue for long OCR jobs), and interleave its events with the legitimate attempt's. Add locked_by (and attempt number) to set_status/append_event with WHERE locked_by=$worker AND status='running'; a 0-row fenced write cancels the local task immediately.

3. FIX THE ACCEPT PATH. (a) Run the idempotency pre-check (di/routers/ingest.py:78-83) BEFORE the backpressure check — as designed, a retry of an already-accepted submit can 429 under load, breaking the idempotency contract. (b) The global queued cap is unimplementable as specified: the accept path runs under the tenant GUC (di/db.py:91-92) and di_job is FORCE-RLS (005_hardening.sql:163-173), so a "global" count silently returns only that tenant's rows. It appears to work in compose only because RLS_ENABLED=false and the compose 'di' user is the bootstrap superuser (docker-compose.yml:13,39), which bypasses FORCE RLS. Either implement the global count via a SECURITY DEFINER function / dedicated role, or drop the global cap and keep only per-tenant caps.

4. USE A DEDICATED DB ROLE (TO di_worker) FOR THE QUEUE POLICY NOW, not the GUC-trust queue_worker_access policy. The design itself admits a security reviewer may demand this, and the known RLS-role-split upgrade will force the rework anyway — shipping the GUC version buys one migration plus one security re-review for nothing.

5. THREAD THE AUTHORITATIVE VERSION THROUGH. The advisory-lock fix re-decides version_no inside create_version, but _base_path uses the stale pre-lock plan.version_no for ltree paths (di/pipeline.py:247) and the done event / supersedes_id use the stale pre-lock read (:244, :287). create_version must return (version_no, supersedes) decided under the lock, and base path + events must be computed after it — otherwise knode paths say v2 while doc_version says v3.

6. SERIALIZE PER-CLIENT REMERGE. With tenant_max_running=4, two workers finishing different docs for one client run _remerge_client_facts concurrently (di/pipeline.py:84-118); the one holding the staler fact snapshot can win the upsert (UNIQUE at 002_core_tables.sql:74) and publish a merged view missing the newer doc's facts until the next ingest. Take pg_advisory_xact_lock on client_id around remerge, or coalesce into a per-client 'remerge' job kind. Coordinate with the multi-valued-facts upgrade, which changes exactly the UNIQUE this design leans on for idempotency.

7. DE-RISK MIGRATION 006. The runner executes each file as one asyncpg multi-statement execute (di/db.py:214) = one implicit transaction, so all ACCESS EXCLUSIVE locks (di_job ALTERs + the non-concurrent CREATE UNIQUE INDEX on doc_version) are held until the whole file finishes, while old replicas are still writing both tables mid-rolling-deploy; CREATE INDEX CONCURRENTLY is impossible under this runner. Split 006 into 006a (di_job queue columns, cheap) and 006b (doc_version unique index), and sequence deploys: ship the create_version retry/lock code one release BEFORE the unique index lands, because old-code replicas have no UniqueViolation handling in create_version (di/store.py:179-186) and will hard-fail in-flight ingests the moment the index exists. For long-lived environments document an out-of-band CREATE UNIQUE INDEX CONCURRENTLY + ledger-entry procedure.

8. UNIFY THE TERMINAL TAXONOMY. As specified, attempts exhausted via in-process exception ends 'failed' while attempts exhausted via lease expiry ends 'dead' — the page-worthy dead>0 alert misses every deterministic poison document that fails cleanly. Attempts-exhausted should always end 'dead'; reserve 'failed' for non-retryable classification.

9. API CONTRACT HYGIENE. Exclude payload from _JOB_COLS/Job — it carries internal blob URIs and file paths that must not reach API callers. Document that new status values ('dead','canceled') in an existing response field ARE breaking for strict-enum clients (the "API contract unchanged" claim needs this caveat). Either add the cancel endpoint or drop 'canceled' from 006. Rollout step 4 must be rewritten: pre-006 orphaned rows have payload '{}' and their bytes died with the old process — they are claimed-and-failed with an explicit "payload lost pre-upgrade" error, not "rescued"; the worker must special-case empty payload rather than KeyError.

10. WORKER MECHANICS. Claim at most min(batch, free semaphore slots) so claimed-but-locally-queued jobs don't burn lease unheartbeated (or get held hostage by a stuck worker if heartbeated). LISTEN needs a dedicated non-pool connection (the pool recycles idle connections at 300s, di/db.py:69). Add ORDER BY to the locking subquery (order after IN + SKIP LOCKED is unspecified, so the round-robin ranking is not actually enforced at the lock step) and document the per-tenant running cap as soft (the NOT IN anti-join is racy across concurrent claimers). Honestly, ship plain SKIP LOCKED FIFO + soft per-tenant cap as v1 and add the window-function fairness query only when measured — it is the most complex, least-tested piece for a small team.

11. OBSERVABILITY + COMPLIANCE. Queue depth/age gauges refreshed only by workers go stale exactly when all workers are down — export queue_stats from the API process too, or alert on metric staleness. For a bank, "optional weekly GC" of orphaned PII blobs is not acceptable: make blob GC a scheduled, audited job kind on the same queue, define a retention TTL for dead-job payload blobs, and extend tenant offboarding (purge_client_jobs, di/jobs.py:305-317 + BlobStore.delete_client) to cancel running work and purge queued/dead job payload blobs. Flip ingest_embedded_worker default to False when settings.is_production so a forgotten env var cannot silently recreate option (c)'s API/ingest coupling. Note blob_backend=local requires a shared RWX volume across API and workers on k8s — effectively mandate postgres/s3 for multi-node.

---

## 2. Multi-valued facts (directors, beneficial owners, accounts)

**Effort:** L · **Reviewer verdict:** needs_changes (corrections below are normative)

### Current gap
client_merged_fact enforces UNIQUE (client_id, attribute_key) (di/migrations/002_core_tables.sql:74), and the merge collapses every attribute to exactly one winner: merge_facts groups by attribute_key and picks max-confidence (di/subtree/merge.py:96-106), so a corporate client's three directors extracted from an Acta Constitutiva (ontology.py:188-193 declares MX_ACTA_CONSTITUTIVA yields ownership.director) become ONE row flagged conflict=true/needs_review=true — the other two directors are unrepresentable. Adjudication is likewise single-slot: di_fact_adjudication UNIQUE (client_id, attribute_key) (005_hardening.sql:91), upserted at store.py:293-310 and re-applied keyed by attribute_key alone (pipeline.py:104-112, merge.py:128), so a reviewer cannot accept one director and reject another, and 'reject' nulls the whole attribute (merge.py:155-162). The API (routers/clients.py:98-109), serving projection (serving.py:118-139) and console table (frontend/src/pages/Facts.tsx:132 keys rows by attribute_key) all assume one row per attribute_key. There is no cardinality concept anywhere in di/ontology.py (ATTRIBUTE_KEYS is a flat dict[str,str], ontology.py:15-54).

### Options considered

**A. Per-instance rows keyed by deterministic value-fingerprint instance_key (recommended)** — Add instance_key text NOT NULL DEFAULT '' to client_merged_fact and di_fact_adjudication; new uniqueness (client_id, attribute_key, instance_key). Multi-cardinality keys (declared in di/ontology.py) are sub-grouped in merge.py by a normalized-value fingerprint (NFKD accent-fold + casefold + whitespace-collapse, sha256[:16]); single-valued keys keep instance_key='' so their behavior is byte-identical. Adjudication, serving and the API gain instance_key additively.

- ✅ Single-valued path is EXACTLY unchanged: sentinel '' rows hit the same one-row-per-key uniqueness and the same upsert shape
- ✅ Deterministic and explainable — the fingerprint's normalized input is stored in resolution_rationale, so an auditor can see exactly why two mentions merged
- ✅ Per-instance adjudication (accept/override/reject one director) falls out of the same keying; adjudications survive re-merge because fingerprints are stable
- ✅ Relational rows: RLS policies, masking, verified_only, purge_client all apply per instance with zero new machinery
- ✅ No new infrastructure; migration is 8 lines of idempotent DDL
- ❌ Value-identity is literal: 'J. Perez' vs 'Juan Perez' become two instances (reviewer rejects one) — no fuzzy matching in v1
- ❌ Rolling-deploy window: old replicas' ON CONFLICT (client_id, attribute_key) errors once the old constraint is dropped (mitigable, see risks)
- ❌ Facts API can now return >1 row per attribute_key for multi keys — consumers keying a map on attribute_key must be notified (single keys unaffected)

**B. JSONB instances array on the existing single row** — Keep UNIQUE (client_id, attribute_key); add an instances jsonb column holding the per-instance resolutions; resolved_value keeps the top winner for compatibility.

- ✅ Zero uniqueness/migration risk; old API shape fully preserved
- ✅ No rolling-deploy constraint hazard
- ❌ Adjudication still has no addressable instance identity — you end up inventing instance_key inside JSON anyway, minus DB enforcement
- ❌ Masking/verified/conflict logic in serving.py:118-139 must be reimplemented per JSON element; two code paths forever
- ❌ JSONB blobs are opaque to SQL audit queries, indexes and future reporting — poor fit for a bank's compliance queries
- ❌ instances array rewritten wholesale each merge: worse audit granularity, no per-instance updated_at

**C. Entity-resolution instances linked to di_entity** — Instances are entities: run person/org resolution (fuzzy name match, DOB/role discriminators) into di_entity (002_core_tables.sql:51-59) and key merged facts by entity id.

- ✅ Handles spelling variants and 'same name, different person' correctly
- ✅ Reuses the existing di_entity table; the long-term right model for UBO graphs
- ❌ Fuzzy matching is neither deterministic nor easily explainable — unacceptable for v1 in a KYC audit context ('why did the system decide these are the same director?')
- ❌ Big scope: ER thresholds, tuning, backfill, review tooling; blocks a needed fix behind a research project
- ❌ Non-deterministic instance identity breaks the adjudication-survives-re-merge invariant unless ER output is frozen, which adds its own state

**D. Child table client_merged_fact_instance** — Keep client_merged_fact as a per-attribute_key summary row; add a child table holding instances FK'd to it.

- ✅ client_merged_fact contract untouched; summary row can carry aggregate flags
- ✅ Clean place for per-instance columns
- ❌ Two tables to keep transactionally consistent on every merge + a new RLS policy + purge/delete path
- ❌ Facts API must join or make two queries; summary row semantics for multi keys (what is ITS resolved_value?) are awkward and misleading
- ❌ More moving parts than A for the same expressive power — fails the 'boring, pays for itself' test

### Recommendation
Option A. For a bank the deciding criteria are determinism, explainability, and least mechanism. A value-fingerprint instance_key is a pure function of the extracted value — any auditor can recompute it, the normalized input is stored alongside the row, and re-merges reproduce identical keys forever, which is precisely what makes per-instance adjudications durable (the same property that makes today's attribute_key-keyed adjudications work, pipeline.py:104-117). It keeps everything relational so the existing FORCE-RLS policies (004/005 pattern), masking projection (serving.py:118-139), verified semantics, and purge_client (store.py:516-528) apply per instance with no new code paths. The sentinel-'' design means single-valued attributes — the overwhelming majority at millions-of-clients scale — execute the exact same SQL and produce the exact same rows as today, so the blast radius is confined to the four declared multi keys. Option B hides the problem inside JSON and forfeits DB-enforced adjudication identity; C is the right v2 but is not deterministic enough to ship first; D adds a table and a join for nothing A can't do. A is also the cleanest stepping stone to C: when entity resolution arrives, it becomes a smarter fingerprint function behind the same (client_id, attribute_key, instance_key) contract.

### Design (as proposed)
## Design: multi-valued facts via (client_id, attribute_key, instance_key)

### 1. Cardinality declaration — di/ontology.py

Add below ATTRIBUTE_KEYS (ontology.py:15-54), keeping the existing dict untouched (it is consumed as data elsewhere):

```python
# Attributes that may legitimately hold several concurrent values per client.
# Everything not listed is single-valued (fail-closed to existing behavior).
MULTI_VALUED_ATTRIBUTE_KEYS: frozenset[str] = frozenset({
    "ownership.director",
    "ownership.beneficial_owner",
    "ownership.authorized_signer",
    "account.number",
})

def cardinality_for(attribute_key: str) -> str:
    """'multi' | 'single'. Unknown keys default to 'single'."""
```

Unknown/extractor-invented keys default single — no behavior change unless a key is explicitly promoted. Promoting a key later is safe (see §4 backfill); demoting requires an ops runbook (not supported in v1).

### 2. Instance identity — deterministic fingerprint (v1)

New pure helper in di/subtree/merge.py:

- `_identity_basis(f: FactInput) -> str | None`: primary identity dimension = value_text if set, else value_date.isoformat(), else repr of value_num (`f"{value_num:.10g}"`); None if all empty.
- Normalization (versioned as `identity_algo = "nfkd-casefold-ws-v1"`): `unicodedata.normalize("NFKD", s)` → drop combining marks → `casefold()` → collapse internal whitespace → strip. This deliberately extends the existing `_comparable_key` normalization (merge.py:55-66) with accent folding so "Juan Pérez Gómez" and "JUAN PEREZ GOMEZ" are the same director.
- `instance_fingerprint(f) -> str` = `hashlib.sha256(normalized.encode()).hexdigest()[:16]` (64 bits; collision within one client+key group of a handful of instances is negligible).
- Explainability: each merged row's resolution_rationale gains `{"instance_key": ..., "identity_basis": <normalized string>, "identity_algo": "nfkd-casefold-ws-v1", "instance_count": <siblings for this key>}`. The algo string is immutable; a future smarter matcher ships as v2 with an explicit re-adjudication plan, never by mutating v1.
- Two mentions are the same instance iff fingerprints are equal. "J. Perez" vs "Juan Perez" → two instances; the reviewer rejects the spurious one. Deterministic, recomputable, explainable — accepted v1 limitation, documented.
- Candidates under a multi key whose comparable value is entirely empty (`_is_empty`, merge.py:69-70) are excluded from instance formation (they carry no identity and today only contribute noise).

Single-valued keys: `instance_key = ""` (sentinel, never NULL — NULL would break the unique index and the upsert conflict target).

### 3. Migration — di/migrations/006_multi_valued_facts.sql

New file only (the runner's checksum ledger, di/db.py:186-233, forbids editing 001-005; 006 follows the __SCHEMA__ + idempotency conventions and, like 005, is pure DDL because DML under the runner would be RLS-filtered — see 005_hardening.sql:20-22). The runner applies it under the advisory lock at startup (db.py:200), so concurrent replicas cannot race it.

```sql
-- 006_multi_valued_facts.sql — multi-cardinality attributes: per-instance merged rows and
-- per-instance adjudication. instance_key is '' (sentinel) for single-valued attributes, so
-- their uniqueness and upsert behavior are unchanged. Idempotent; pure DDL (005 note applies:
-- DML here would be RLS-filtered — backfill happens via application re-merge, which binds the
-- tenant GUC). Existing rows adopt instance_key '' via the column DEFAULT; the first re-merge
-- of a client rewrites multi-key rows with real fingerprints and deletes the stale '' row.

ALTER TABLE __SCHEMA__.client_merged_fact
    ADD COLUMN IF NOT EXISTS instance_key text NOT NULL DEFAULT '';

-- Create the replacement uniqueness BEFORE dropping the old one: no window without a
-- uniqueness guarantee, and the new index is the ON CONFLICT arbiter for the new writer.
CREATE UNIQUE INDEX IF NOT EXISTS client_merged_fact_client_attr_instance
    ON __SCHEMA__.client_merged_fact (client_id, attribute_key, instance_key);

-- Auto-generated name from 002_core_tables.sql:74 UNIQUE (client_id, attribute_key).
ALTER TABLE __SCHEMA__.client_merged_fact
    DROP CONSTRAINT IF EXISTS client_merged_fact_client_id_attribute_key_key;

ALTER TABLE __SCHEMA__.di_fact_adjudication
    ADD COLUMN IF NOT EXISTS instance_key text NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS di_fact_adjudication_client_attr_instance
    ON __SCHEMA__.di_fact_adjudication (client_id, attribute_key, instance_key);

-- Auto-generated name from 005_hardening.sql:91 UNIQUE (client_id, attribute_key).
ALTER TABLE __SCHEMA__.di_fact_adjudication
    DROP CONSTRAINT IF EXISTS di_fact_adjudication_client_id_attribute_key_key;
```

No new tables → no new RLS work: both tables already carry FORCE RLS tenant_isolation policies (004_rls.sql pattern; di_fact_adjudication policy created at 005_hardening.sql:156-174), and the new column inherits them. purge_client already deletes both tables (store.py:519-520).

### 4. Existing-row migration path / backfill

- Post-006, every existing row has instance_key='' and remains valid (single keys: forever; multi keys: until re-merged).
- Re-merge is the backfill. It already runs on every ingest (pipeline.py:282) and every adjudication (routers/admin.py:115). The new full-set replace semantics (§6) delete the stale '' row for a multi key when the fingerprinted rows land.
- Proactive backfill for dormant clients: new `tools/remerge_backfill.py` — pages client_ids from client_merged_fact where attribute_key = ANY(multi_keys) AND instance_key = '', calls `_remerge_client_facts` per client (one txn each, bounded concurrency, resumable by client_id cursor). At millions of clients this is optional hygiene, not a correctness requirement: the collapsed rows are no worse than today until touched.

### 5. Merge — di/subtree/merge.py

Signature (pure module stays pure — ontology knowledge is passed in):

```python
def merge_facts(facts, client_id="",
                adjudications: dict[tuple[str, str], Adjudication] | None = None,  # (attribute_key, instance_key)
                ontology_version=None,
                multi_keys: frozenset[str] = frozenset()) -> list[ClientFact]
```

- `Adjudication` gains `instance_key: str = ""` (merge.py:43-52); `ClientFact` gains `instance_key: str = ""` (di/models.py:218-236).
- Single keys (`attribute_key not in multi_keys`): existing algorithm verbatim (merge.py:96-129), instance_key='', adjudication looked up at `(key, "")`. Output identical to today.
- Multi keys: group by attribute_key, then sub-group by `instance_fingerprint`. Each sub-group is resolved with the SAME rules as a single group: winner = max confidence, first-wins tiebreak (merge.py:100); `conflict` = >1 distinct non-empty `_comparable_key` tuple WITHIN the sub-group (fingerprint covers only the primary dimension, so members can still disagree on value_date/value_num — e.g. two documents assert different appointment dates for the same director); `needs_review = conflict`, same as merge.py:106-116.
- **needs_review now means**: "sources disagree within this instance (or within this single-valued attribute)". Three directors with clean values are three rows, conflict=false — the current false-positive 'conflict' disappears. Cross-instance plurality is NOT a conflict; it is surfaced via `instance_count` in the rationale and the API so reviewers can spot suspicious proliferation (e.g. 7 near-duplicate spellings) and reject spurious instances.
- Adjudication application per row via `adjudications.get((attribute_key, instance_key))`:
  - accept / override: unchanged semantics (merge.py:143-154), applied to that instance only. Note: an override changes the displayed value but the row KEEPS its original instance_key — instance_key is a sticky identity handle, not re-derived from the overridden value.
  - reject on a MULTI instance: the instance row is omitted from the output entirely ("remove a spurious director"); the durable audit record is the di_fact_adjudication row itself (exposed via the new list endpoint, §8). Because fingerprints are deterministic, every future re-merge re-derives the same instance_key and drops it again.
  - reject on a SINGLE key: existing behavior verbatim (row kept, values nulled, needs_review=true, merge.py:155-162) — unchanged by requirement.
- Output ordering: sort by (attribute_key, instance_key) for determinism.

### 6. Store — di/store.py

- `upsert_merged_facts` (store.py:252-280) becomes **`replace_merged_facts(client_id, facts)`** with explicit full-client-set contract, in ONE transaction:
  1. executemany upsert including instance_key, `ON CONFLICT (client_id, attribute_key, instance_key) DO UPDATE ...` (same SET list as store.py:265-273).
  2. Delete stale rows (removed instances, rejected multi instances, and the pre-006 collapsed '' rows for multi keys):
     `DELETE FROM "{s}".client_merged_fact WHERE client_id = $1 AND NOT EXISTS (SELECT 1 FROM unnest($2::text[], $3::text[]) AS keep(a, i) WHERE keep.a = attribute_key AND keep.i = instance_key)` (parallel arrays of the new set's keys). This also fixes the latent bug that deleting a document never removed its now-orphaned merged rows (delete_document docstring says the caller recomputes, store.py:493-499, but recompute today never deletes).
  All callers (pipeline.py:117, hence also admin adjudicate) already pass the full client set, so semantics are safe.
- `upsert_adjudication` (store.py:293-310): add `instance_key: str = ""` param; INSERT it; conflict target `(client_id, attribute_key, instance_key)`.
- `fetch_adjudications` (store.py:286-290): unchanged query; caller now reads instance_key from rows.
- `fetch_merged_facts` (store.py:409-419): `ORDER BY attribute_key, instance_key`.

### 7. Pipeline — di/pipeline.py

`_remerge_client_facts` (pipeline.py:84-118): build the adjudication map keyed by `(r["attribute_key"], r.get("instance_key") or "")` (replacing the str keying at 105-112) and pass `multi_keys=ontology.MULTI_VALUED_ATTRIBUTE_KEYS` into merge_facts. No other pipeline change; the stage-event generator contract is untouched.

### 8. API — additive, no version bump

- `GET /api/v1/clients/{client_id}/facts` (routers/clients.py:98-109): response model unchanged (facts is list[dict], clients.py:35-39). Each row additionally carries `instance_key` (always present; '' for single keys) and `instance_count` (per attribute_key, computed in serving). Multi keys may now return several rows per attribute_key; `?attribute_key=ownership.director` returns all instances. Single-valued keys keep exactly one row — the previous behavior — so this is additive for the dominant case and a documented semantic fix for multi keys (the old single collapsed 'conflict' row was wrong, not a contract worth preserving). Release notes + OpenAPI description flag it; no /v2 needed.
- `POST /api/v1/admin/clients/{client_id}/adjudicate` (routers/admin.py:99-117): `AdjudicationRequest` (admin.py:36-43) gains `instance_key: str = ""`. Fail-closed validation: if `cardinality_for(attribute_key) == "multi"`, instance_key must be non-empty AND must reference an existing merged row (fetch and 404 otherwise — prevents typo'd fingerprints silently doing nothing); if single, instance_key must be '' (422 otherwise). Existing callers (single keys, no instance_key field) are untouched.
- New `GET /api/v1/admin/clients/{client_id}/adjudications` (admin scope, authorize_client): lists di_fact_adjudication rows so rejected (hence invisible) instances remain auditable.

### 9. Serving / masking — di/serving.py

- `project_facts` (serving.py:118-139): after projection, compute `instance_count` per attribute_key over the result set and stamp each row; pass instance_key through. Masking (`_redact`, value_date/value_num nulling at 131-137) applies per row unchanged — each director row masks independently. `is_verified` (100-115) applies per instance: one human-verified director does not launder its siblings.
- `sensitivity_for_key` (serving.py:142-149) currently maps `ownership.*` to LOW — beneficial-owner and director identities are personal data. Add `"ownership."` to the HIGH prefix tuple at :147 so multi-instance owner rows are masked by the default policy. (account.number is already HIGH via "account.".)

### 10. Config / compose

- di/config.py:81: bump `ontology_version` default "1.0.0" → "1.1.0" (merged rows are stamped with it, pipeline.py:115, so vintage is auditable). No new settings — cardinality is ontology data, not deployment config.
- docker-compose.yml / Dockerfile: NO changes. 006 auto-applies via the startup migration runner; the demo path is unaffected.

### 11. Frontend — frontend/src/console

- Facts.tsx: row key changes from `f.attribute_key` (Facts.tsx:132) to `` `${f.attribute_key}:${f.instance_key}` ``; group consecutive rows of the same attribute_key (first row shows the key + an "N instances" badge when instance_count > 1, siblings indent); sources drawer title includes the instance value; adjudication affordance passes instance_key. lib/types.ts fact type gains instance_key/instance_count.

### 12. Failure modes handled

- **Rolling deploy (old code + new schema)**: after 006 drops the old constraint, an old replica's `ON CONFLICT (client_id, attribute_key)` (store.py:264) raises "no unique constraint matching". Blast radius: the merge stage of in-flight ingests on old replicas — the job records failed with the error (di_job events) and is retriable; re-merge is idempotent from source facts. For compose/demo (single instance) this is moot. For strict zero-downtime production, split the constraint drops into 007 shipped one release after the code that writes instance_key (old constraint stays satisfied meanwhile because old code's rows default to ''; new multi-instance writes are simply deferred to release N+1). Design ships the single-006 path with this documented as the ops choice.
- **Fingerprint drift**: OCR improvement changes a spelling → new fingerprint → a prior reject no longer matches → the instance REAPPEARS flagged for review. Fails open toward human review — the safe direction for KYC. identity_algo is versioned and immutable.
- **Adjudication targeting a vanished instance** (source doc deleted): the stored verdict simply stops matching at merge time — harmless, still auditable via the adjudications endpoint.
- **Two genuinely distinct directors with identical normalized names**: collapse into one instance. Documented v1 limitation; v2 (entity resolution with DOB/role discriminators, Option C) slots in as a new fingerprint version behind the same schema.
- **Merge crash mid-replace**: single transaction — either the full new set (upserts + stale deletes) lands or nothing does; readers never observe a half-replaced view.
- **Stale collapsed rows at scale**: remain valid single rows until the client is next touched; optional tools/remerge_backfill.py sweeps them.

### 13. Test plan

Unit (pure, extend tests/test_subtree_merge.py + new tests/test_instance_identity.py):
1. Golden regression: every existing single-key merge test passes UNCHANGED with multi_keys=frozenset() and with the real multi set (proves single path untouched, instance_key == "").
2. Fingerprint determinism: same input → same key across runs; accent/case/whitespace variants ("Juan Pérez Gómez" / "JUAN  PEREZ GOMEZ") → equal; distinct names → distinct.
3. **Acta Constitutiva scenario (pure)**: 3 FactInputs for ownership.director with distinct names → 3 ClientFacts, distinct instance_keys, conflict=false, needs_review=false, instance_count rationale = 3.
4. Same director from two documents (different confidence) → one instance, max-confidence winner, both source_fact_ids.
5. Within-instance secondary conflict: same name, different value_date → one row, conflict=true, needs_review=true.
6. Per-instance adjudication: accept director B (human_verified, others untouched); override director B's value (instance_key unchanged); reject director C → row absent, A and B still present; reject on a single key → legacy null-and-flag behavior byte-identical.
7. Empty-value candidates under a multi key are excluded; under a single key behave as today.
8. cardinality_for: listed keys → multi; unknown → single.

Integration (DI_RUN_INTEGRATION=1):
9. Fresh DB: 001→006 apply; assert new unique indexes exist, old constraints gone, ledger has 006.
10. Upgrade path: apply 001→005, seed a collapsed ownership.director row + an old-style adjudication, apply 006 (checksums 001-005 unchanged in ledger), re-merge → collapsed '' row deleted, fingerprinted rows present, old adjudication (instance_key='') still applies to its single-key facts.
11. replace_merged_facts deletes stale instances after a source document is deleted; concurrent double re-merge (two tasks) converges without unique violations.
12. Adjudicate endpoint validation: multi key without instance_key → 422; nonexistent instance → 404; adjudications list endpoint returns the reject.

End-to-end (tools/smoke_test.py additions, runs against compose):
13. Ingest samples Acta Constitutiva (3 directors) → GET /facts shows 3 ownership.director rows, distinct instance_keys, none conflicted, instance_count=3, masked by default (post ownership.→HIGH change); POST adjudicate reject one instance → GET /facts shows 2; re-ingest the same document → still 2 (reject survived re-merge); GET adjudications shows the reject.
14. Existing 47 smoke checks still green (single-key contract intact).

### Code touchpoints
- `di/migrations/006_multi_valued_facts.sql (new)`
- `di/ontology.py:54 (add MULTI_VALUED_ATTRIBUTE_KEYS + cardinality_for after ATTRIBUTE_KEYS)`
- `di/subtree/merge.py:29-52 (FactInput unchanged; Adjudication + instance_key), :55-66 (_comparable_key reused), :73-130 (merge_facts: multi_keys param, fingerprint sub-grouping, tuple-keyed adjudications), :133-162 (_apply_adjudication: per-instance reject-removes)`
- `di/models.py:218-236 (ClientFact.instance_key)`
- `di/store.py:252-280 (upsert_merged_facts → replace_merged_facts: new conflict target + stale-delete in txn), :293-310 (upsert_adjudication instance_key), :409-419 (fetch_merged_facts ordering)`
- `di/pipeline.py:104-116 (_remerge_client_facts: tuple-keyed adjudications, pass multi_keys)`
- `di/routers/admin.py:36-43 (AdjudicationRequest.instance_key), :99-117 (validation + instance existence check), new GET adjudications route`
- `di/routers/clients.py:98-109 (docstring/OpenAPI note; response rows gain instance_key/instance_count)`
- `di/serving.py:118-139 (project_facts instance passthrough + instance_count), :142-149 (add ownership. to HIGH)`
- `di/config.py:81 (ontology_version → 1.1.0)`
- `frontend/src/pages/Facts.tsx:132 (row key + grouping + instance badge)`
- `frontend/src/lib/types.ts (fact type fields)`
- `tools/remerge_backfill.py (new, optional ops sweep)`
- `tools/smoke_test.py (Acta 3-director E2E checks)`
- `tests/test_subtree_merge.py + tests/test_instance_identity.py (new cases)`

### Risks
- Rolling-deploy window: old replicas' ON CONFLICT (client_id, attribute_key) fails once 006 drops the old constraint — merge-stage job failures are retriable, but strict zero-downtime shops must split the constraint drop into a follow-up 007 one release later (documented in design §12).
- API consumers that build a map keyed on attribute_key will silently drop all but one director once multi rows appear — requires release-note communication and a scan of known internal consumers before enabling; single-valued keys are unaffected.
- Value-literal identity: 'J. Perez' vs 'Juan Perez' yields two instances (reviewer burden) and two distinct people with identical normalized names yields one (under-count) — accepted v1 tradeoff, mitigated by per-instance reject and the versioned identity_algo upgrade path to entity resolution.
- Fingerprint reappearance: an OCR/extractor improvement that changes a spelling resurrects a previously rejected instance for re-review — fails open to human review (safe), but could surprise reviewers at volume.
- The new stale-delete in replace_merged_facts changes upsert-only semantics to full-set replace; any future caller passing a PARTIAL fact set would silently delete the rest — enforce via docstring, name, and an integration test asserting full-set contract.
- Constraint names in 006 rely on Postgres auto-generated names from 002/005 table constraints (client_merged_fact_client_id_attribute_key_key, di_fact_adjudication_client_id_attribute_key_key); verify against a live pre-006 database during implementation (deterministic in stock PG, but cheap to confirm).
- Adding ownership.* to HIGH sensitivity changes masking defaults for existing consumers of director facts — compliance-positive but must be flagged in the release notes alongside the multi-value change.

### Adversarial review — corrections (normative)

**Factual errors found in the proposal:**
- The design claims "the runner's checksum ledger (di/db.py:186-233) forbids editing 001-005." It does not forbid anything: db.py:209-211 skips a file only when its checksum MATCHES the ledger; a mutated 001-005 file is silently RE-EXECUTED and the ledger overwritten via ON CONFLICT DO UPDATE (db.py:218-224). The docstring (db.py:191-192) even advertises this as a feature. The conclusion (ship 006 as a new file) is right, but the stated enforcement mechanism is wrong — a mutated old file is a re-run hazard, not a blocked operation.
- §11 says the Facts console 'adjudication affordance passes instance_key' — there is no adjudication affordance anywhere in the frontend today (grep for 'adjudicat' across frontend/src returns zero hits; Facts.tsx renders read-only rows, Facts.tsx:129-187). Adjudication is API-only (routers/admin.py:99-117). The frontend line item is new UI construction, not a parameter-plumbing change; the L effort estimate should absorb that or the item should be cut.
- Test-plan item 14 claims 'existing 47 smoke checks'; tools/smoke_test.py contains 39 check( call sites. Not load-bearing, but a reviewer told to verify every claim should not find the very first count wrong.
- Minor citation drift: 'accept / override: unchanged semantics (merge.py:143-154)' — override is 142-149 and accept 150-154; and 'merge_facts groups by attribute_key and picks max-confidence (merge.py:96-106)' — the winner selection is line 100 and the conflict computation 102-106. Everything else cited (002_core_tables.sql:74, 005_hardening.sql:91 and :156-174, store.py:252-280/264/293-310/409-419/493-499/516-528, pipeline.py:104-117/282, serving.py:118-139/142-149, ontology.py:15-54/188-193, admin.py:36-43/99-117, clients.py:98-109, config.py:81, models.py:218-236, db.py:200, Facts.tsx:132) checks out exactly.

**Design flaws to fix:**
- LOST-DELETION RACE UNDER CONCURRENT RE-MERGE (worst flaw; directly contradicts the durable PG-queue-workers upgrade). The runner's semaphore is global, not per-client (ingest_runner.py:29-36, ingest_concurrency=4, config.py:60), so two documents for the SAME client already ingest concurrently on one instance, and the queue-workers upgrade makes it cross-replica. Today's upsert-only merge (store.py:252-280) makes this race merely stale. The new replace_merged_facts stale-DELETE makes it destructive: job A snapshots fetch_client_facts before job B's knodes commit; B remerges and commits its instances; A's replace then commits with a keep-set that lacks B's rows and DELETEs them. The merged view silently loses whole attributes/instances until the client is next touched. Integration test 11 ('concurrent double re-merge converges without unique violations') tests the wrong invariant — absence of unique violations is not convergence.
- CONFLICT-NORMALIZATION INCONSISTENCY reintroduces the false-positive the design claims to eliminate. The fingerprint folds accents (NFKD, §2) but within-instance conflict is computed over _comparable_key (merge.py:55-66), which casefolds and collapses whitespace but does NOT fold accents. 'Juan Pérez' (Acta) + 'Juan Perez' (INE OCR) merge into one instance yet permanently flag conflict=true/needs_review=true — systematic for Mexican documents, the design's own motivating corpus. The primary-dimension comparison inside a sub-group must use the identity normalization itself.
- REJECT IS A ONE-WAY DOOR. §8 validation requires instance_key to 'reference an existing merged row (404 otherwise)', but a multi-key reject removes that row from client_merged_fact. The reviewer who rejected the wrong director cannot then accept/override/re-adjudicate it: every subsequent verdict 404s. There is also no endpoint to clear an adjudication at all (today or in the design). Validation must also accept an instance_key that matches an existing di_fact_adjudication row, and a clear-verdict operation is required.
- "THE DURABLE AUDIT RECORD IS THE ADJUDICATION ROW" IS FALSE. upsert_adjudication is ON CONFLICT DO UPDATE and resets created_at=now() (store.py:304-307); the design keeps this shape with instance_key added. A second verdict on the same instance silently destroys the reject record and falsifies its timestamp. For a bank, adjudications need append-only history (or an immutable audit sidecar); the new GET adjudications endpoint only exposes the current mutable row.
- EMPTY-SET REPLACE IS UNSPECIFIED AND DEFAULTS TO THE BUG BEING FIXED. upsert_merged_facts early-returns when facts is empty (store.py:253-255). If replace_merged_facts inherits that guard, deleting a client's last document leaves the entire stale merged view — exactly the orphaned-rows bug the design claims to fix via stale-delete. The contract must state: empty set deletes ALL merged rows for the client (the explicit client_id parameter makes this possible; facts[0].client_id does not).
- MASKING LEAK VIA RATIONALE AND FINGERPRINT. §2 stores the cleartext normalized value ('identity_basis' — a director's full name) in resolution_rationale, and project_facts passes rows through masking only resolved_value/value_date/value_num (serving.py:127-137) — resolution_rationale rides through untouched. So the moment ownership.* is promoted to HIGH (§9), every masked response still carries the director's name in cleartext inside the rationale. Separately, instance_key is an unsalted sha256[:16] of the normalized value: names are low-entropy, so anyone with API access can dictionary-confirm 'is Juan Perez a director of client X' from a fully masked response. Redact identity_basis under mask, and either HMAC the fingerprint with a stable deployment secret (documented for auditors) or explicitly accept and document the inference channel.
- THE TWO-PHASE 006/007 ALTERNATIVE HAS NO MECHANISM. In the interim release the old UNIQUE (client_id, attribute_key) still exists; new code inserting a second instance row for a multi key raises a unique violation on the OLD constraint (it is not the ON CONFLICT arbiter, so it is an error, not an upsert). 'New multi-instance writes are simply deferred to release N+1' names no gate: it needs an explicit feature flag (multi_keys forced empty until 007 is in the ledger) or a startup probe that the old constraint is gone. Without that, release N fails in the mirror image of the single-006 rolling-deploy failure.
- 006'S NON-CONCURRENT CREATE UNIQUE INDEX AT THE DESIGN'S OWN SCALE. client_merged_fact at millions of clients is tens of millions of rows; CREATE UNIQUE INDEX (non-CONCURRENTLY) takes a SHARE lock blocking all writes for the build, inside the startup path under the advisory lock (db.py:200). A readiness/liveness timeout can kill the pod mid-build; the runner executes each file as one multi-statement script (db.py:212-214, implicit transaction), so the build rolls back and the deploy crash-loops, rebuilding from zero each attempt. CREATE INDEX CONCURRENTLY cannot run there (no transaction allowed). The design needs an ops story: out-of-band CONCURRENTLY build before the release with 006 reduced to metadata ops, or an accepted write-block window.
- SINGLE→MULTI PROMOTION IS CLAIMED 'SAFE' BUT ORPHANS ADJUDICATIONS. §1 says promoting a key later is safe; but existing adjudications for that key are keyed instance_key='' — after promotion and re-merge, no instance carries '' so a previously rejected or overridden value silently reverts to unadjudicated fingerprinted instances. Promotion needs a runbook step that migrates or voids the '' adjudication and re-queues the key for review.
- instance_count computed 'in serving over the result set' (§9) undercounts when verified_only filters rows inside project_facts (serving.py:124-126 skips before appending) and miscounts on masked-and-filtered projections. Compute it over the pre-filter row set.
- MINOR API/COMPAT GAPS: FactsResponse.count changes meaning (rows, no longer attributes) for multi-key clients — release notes mention map-keyed consumers but not count. The deployed console keying rows by attribute_key (Facts.tsx:132) will render duplicate React keys the moment the API returns two directors — old-frontend/new-API skew during rollout, worth an explicit ordering note. And bumping ontology_version's DEFAULT (config.py:81) is a no-op wherever ONTOLOGY_VERSION is pinned in the environment (pydantic-settings env override), so vintage auditability is not guaranteed by the default bump alone.

**Missing pieces:**
- Per-client concurrency control for re-merge (advisory xact lock) and a correct convergence test — mandatory before the durable PG-queue workers upgrade multiplies same-client parallelism.
- Feature flag / gating mechanism (settings.multi_valued_enabled or ledger probe) for the phased 006/007 deployment path; without it release N raises unique violations on the still-present old constraint.
- A way to clear or reverse an adjudication (endpoint + validation path), since reject now deletes the only row the validation checks against.
- Append-only adjudication history (or event table) — the single mutable row with created_at reset on overwrite (store.py:304-307) cannot serve as the compliance audit record the design leans on.
- Explicit empty-set semantics for replace_merged_facts plus a delete-last-document test; inheriting upsert's early return (store.py:253-255) silently preserves the orphaned-merged-rows bug.
- Masking treatment of resolution_rationale.identity_basis and a decision on fingerprint inference (HMAC with permanent deployment key vs documented risk acceptance) — project_facts (serving.py:127-137) masks only three value fields today.
- Ops plan for building the unique indexes on tens-of-millions-row tables: out-of-band CREATE INDEX CONCURRENTLY, expected lock window, and interaction with startup probes given the runner's advisory-locked implicit-transaction execution (db.py:200, 212-214).
- Runbook for promoting a key single→multi that migrates/voids its instance_key='' adjudications; the design's 'promoting later is safe' is untrue for adjudicated keys.
- Document/knode-level blast radius of adding ownership.* to HIGH: _effective_sensitivity (serving.py:38-47) starts masking director knodes in the tree view and document_sensitivity (serving.py:152-167) raises sensitivity_bucket for newly ingested Actas while previously ingested ones keep LOW — an old/new inconsistency compliance will ask about.
- RLS-role-split interaction: 006 ALTER TABLE requires table ownership; the design should state that migrations run under the migrator/owner role, not the future least-privilege runtime role, or the startup runner breaks the moment the role split lands.
- SDK/type updates beyond the console: FactsResponse.count semantics, MergedFact.instance_key/instance_count in frontend/src/lib/types.ts:116-129, and OpenAPI examples showing multiple rows per attribute_key.
- A guard in run_migrations that refuses to start when an applied migration file's checksum changed — the current runner re-executes mutated files, which is the real enforcement gap behind the 'editing old files is forbidden' convention.

**Corrected design deltas:**
1) Serialize re-merge per client: take pg_advisory_xact_lock(hashtext(pg_schema || ':' || client_id)) as the first statement of replace_merged_facts's transaction (and document that the coming PG-queue workers must additionally prefer per-client job affinity). Rewrite integration test 11 to assert NO LOST ROWS after concurrent double re-merge (compare final merged set to a serial re-merge), not merely absence of unique violations. 2) Use one normalization for identity and conflict on the primary dimension: inside a multi-key sub-group, compute the conflict set over (identity-normalized text, value_date, value_num) — i.e. reuse the nfkd-casefold-ws-v1 function, not _comparable_key — so accent variants of the same director do not flag conflict. Add a unit test: 'Juan Pérez' + 'Juan Perez' → one instance, conflict=false. 3) Fix reject reversibility: adjudicate validation passes if instance_key matches EITHER an existing merged row OR an existing adjudication row for that (client_id, attribute_key); add DELETE /api/v1/admin/clients/{id}/adjudications/{attribute_key}/{instance_key} (admin scope) that removes the verdict and re-merges. 4) Make adjudication audit real: add an append-only di_fact_adjudication_event table (or a history jsonb array written before overwrite) in 006, plus updated_at on the live row; stop resetting created_at on conflict. 5) Specify replace_merged_facts(client_id, facts) MUST NOT early-return on empty facts — empty set deletes every merged row for the client; add the delete-last-document integration test. 6) Close the masking leak: in project_facts, when mask and sensitivity in _MASKABLE, strip identity_basis (or the whole instance sub-dict) from resolution_rationale; decide explicitly on fingerprint inference — either HMAC-SHA256 with a permanent deployment-scoped key from Secret Manager (auditors recompute with the key; key rotation forbidden, documented) or a written risk acceptance for the unsalted hash. 7) Pick ONE deploy path and mechanize it: if single-006, state the accepted merge-stage failure window; if 006/007 split, add settings.multi_valued_enabled defaulting false in release N (multi_keys=∅ when false) and flip in N+1 alongside 007 — the current 'writes are simply deferred' has no implementation. 8) Add the scale ops plan for the index: for large deployments, build both unique indexes out-of-band with CREATE UNIQUE INDEX CONCURRENTLY before rolling the release (006's IF NOT EXISTS then no-ops); document expected lock time for the in-band path and the crash-loop hazard with startup probes. 9) Downgrade the promotion claim: promoting a key single→multi requires voiding/migrating its instance_key='' adjudications and flagging affected clients for re-review; write the runbook now, don't assert 'safe'. 10) Compute instance_count over pre-filter rows in project_facts. 11) Correct the migration-runner rationale (ledger re-executes mutated files rather than forbidding edits — the 'new file only' rule is convention enforced by review, and worth a real guard: refuse to boot when an applied filename's checksum changed, which is a 5-line addition to run_migrations worth bundling into this work). 12) Scope the frontend work honestly: the console has no adjudication UI today; either build it (adds effort) or drop §11's adjudication line and keep only row-key/grouping/badge changes, and note the old-console duplicate-React-key skew during rollout.

---

## 3. RLS production posture: role split & fail-closed guards

**Effort:** M · **Reviewer verdict:** needs_changes (corrections below are normative)

### Current gap
The platform's headline tenant-isolation control — FORCE RLS policies on all 10 tenant tables (di/migrations/004_rls.sql:18-27, 005_hardening.sql:156-174) keyed to the GUC bound in di/db.py:91-92 — is never exercised: docker-compose.yml:35-39 connects as the bootstrap superuser `di` (POSTGRES_USER, docker-compose.yml:13) with `RLS_ENABLED: "false"`, and superusers bypass RLS even with FORCE. There is exactly one credential for everything: the app's runtime pool (di/db.py:55-72) is the same connection that runs owner-level DDL at boot (di/db.py:186-233 — CREATE SCHEMA, migrations, partition creation, ALTER TABLE for vector columns). No startup guard prevents a prod deployment from booting as a superuser with RLS off; startup failures deliberately degrade instead of aborting (di/app.py:81-84,107-109). There is no integration test that proves cross-tenant reads return zero rows or that WITH CHECK rejects cross-tenant writes.

### Options considered

**A — Dual-credential, migrate-on-boot (owner DSN used only inside run_migrations)** — Keep the current 'app applies migrations at startup' flow, but run_migrations() opens a dedicated single connection as di_owner (new PG_MIGRATION_USER/PASSWORD settings) while the runtime pool connects as a new least-privilege di_app role (NOBYPASSRLS). Compose init script creates both roles; RLS_ENABLED flips to true everywhere.

- ✅ Smallest diff to startup flow (di/app.py:105 unchanged in shape); demo stays one `docker compose up`
- ✅ Advisory lock in db.py:200 already serializes concurrent replicas, so multi-instance migrate-on-boot is safe
- ✅ Runtime DDL that needs ownership (partitions db.py:132-139, vector columns db.py:252-274) keeps working unmodified — it just runs on the owner connection
- ❌ Owner credentials live in every app instance's environment — larger blast radius if an app pod is compromised
- ❌ Bank change-management usually requires schema changes as an explicit, approved step, not a side effect of a rolling deploy
- ❌ A bad migration can take down the whole fleet mid-rollout rather than failing one gated job

**B — External migration step + verify-only boot, with mode switch (recommended)** — Same role split as A, plus a MIGRATIONS_MODE setting: `auto` (A's behavior — owner DSN, used by compose/demo), `verify` (prod — app connects ONLY as di_app, never holds owner creds, and refuses to boot if the ledger doesn't match the shipped migration files), `off`. A `python -m di.migrate` entrypoint runs migrations as di_owner from CI/CD or a one-shot job.

- ✅ Prod app instances never possess DDL-capable credentials — true least privilege, cleanest audit story (all DDL is attributable to the migration job)
- ✅ Fail-closed: verify mode turns 'schema drift' into a refused boot instead of silent degraded mode
- ✅ Demo path unchanged (compose runs auto), so the same code serves both postures; no new infrastructure — the migration 'job' is just the existing container with a different command
- ❌ More code: a verify function, a __main__ entrypoint, a mode setting, and two documented operational postures
- ❌ In verify mode, runtime-discovered embedding-dim changes can no longer add vector columns at boot (db.py:231) — dim changes become an explicit migration-step event
- ❌ One extra step in the prod deploy pipeline (run migrate job before rollout)

**C — Minimal: enable RLS in compose, keep single role, add guard only** — Flip RLS_ENABLED to true, create one non-superuser role that both owns the tables and serves traffic (FORCE RLS filters the owner too), add the boot guard. No grant surface, no second DSN.

- ✅ Tiny change; FORCE RLS genuinely filters the owner (004_rls.sql:20), so isolation is exercised
- ✅ No compose or pipeline changes beyond the role
- ❌ The runtime role OWNS the tables, so a SQL-injection or compromised instance can ALTER TABLE ... DISABLE ROW LEVEL SECURITY or DROP POLICY — the control defends against accidents, not attackers; fails a bank security review
- ❌ No least-privilege GRANT story at all (owner has everything, including DROP)
- ❌ Doesn't address migrations-vs-runtime separation the audit will ask about anyway

### Recommendation
Option B. For a bank at multi-instance scale the decisive property is that runtime application instances must not hold credentials capable of disabling the very control (RLS) they are trusted to run under — Option C fails that outright (an owner can DROP POLICY), and Option A leaves owner credentials in every replica's environment forever. Option B gets full least privilege in prod while collapsing to Option A's zero-friction behavior for the compose demo via a single config value, using only boring pieces: two Postgres roles, GRANTs, one new migration file, one guard query against pg_roles/pg_class, and a `python -m di.migrate` entrypoint. It also converts two silent failure modes into refused boots: schema drift (verify mode) and mis-provisioned connections (superuser/BYPASSRLS/RLS-off in prod). The advisory-lock migration runner (db.py:199-233) is reused unchanged, so there is no new coordination mechanism to operate.

### Design (as proposed)
## 1. Role architecture

Two cluster-level roles (names are conventions; the app role name is whatever `PG_USER` is — the migration rewrites a token to it, see §3):

- **di_owner** — `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT`. Owns the schema and every object. Used ONLY by the migration step (`python -m di.migrate`, or the in-app `auto` mode). Needs `GRANT CREATE ON DATABASE document_intelligence` so it can `CREATE SCHEMA` (db.py:197) and create the trusted extensions ltree/pgcrypto/vector (001_extensions.sql:8-9; all three are `trusted = true` on pg16/pgvector images, so no superuser needed).
- **di_app** — `LOGIN NOSUPERUSER NOBYPASSRLS`. Runtime role for the pool in db.py:55-72. Never owns anything; because it is not the table owner AND not BYPASSRLS, both ENABLE and FORCE RLS apply, and it cannot ALTER/DROP policies.

FORCE RLS (004_rls.sql:20, 005:165) stays: it additionally covers the migration connection should a future migration ever run DML (005's header comment, 005_hardening.sql:20-22, already documents this).

Exact privilege surface for di_app, derived from actual runtime DML: SELECT/INSERT/UPDATE/DELETE on tenant tables (INSERTs store.py:138,180,221,241,259,301,480, jobs.py:224; UPDATEs store.py:144,175,264,304, jobs.py:260,291; DELETEs store.py:506-526, jobs.py:316, storage/postgres.py:118,132); `di_api_key`: SELECT (auth.py:188,264,303), INSERT (auth.py:170,297), UPDATE (auth.py:213,232) — **no DELETE** (revocation is soft, auth.py:213); `di_migration_ledger`: SELECT only (needed by verify mode); sequences: USAGE (doc_version.change_seq default, 005:135-139) — nextval requires it.

## 2. New migration: di/migrations/006_grants.sql

Follows the existing conventions: idempotent, `__SCHEMA__` token, plus a new `__APP_ROLE__` token that db.py rewrites to the quoted runtime role (settings.pg_user), exactly parallel to the `__SCHEMA__` rewrite at db.py:212. GRANT/REVOKE are naturally idempotent; the checksum ledger applies the file once (db.py:206-224).

```sql
-- 006_grants.sql — least-privilege runtime role. Idempotent.
-- __APP_ROLE__ is rewritten by di/db.py:run_migrations() to the configured runtime role
-- (settings.pg_user), double-quoted. This file runs as the schema owner (di_owner), so the
-- ALTER DEFAULT PRIVILEGES below binds to the role that creates all future objects.

DO $$
BEGIN
    -- Belt-and-braces for schema-per-env setups: if the runtime role was not pre-created by
    -- the DBA / initdb script, create it NOLOGIN (a DBA must still ALTER ROLE ... LOGIN
    -- PASSWORD out-of-band; we never put a password in a migration).
    IF to_regrole('__APP_ROLE__') IS NULL THEN
        CREATE ROLE __APP_ROLE__ NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END$$;

-- Nobody gets implicit access to tenant data.
REVOKE ALL ON SCHEMA __SCHEMA__ FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA __SCHEMA__ FROM PUBLIC;

GRANT USAGE ON SCHEMA __SCHEMA__ TO __APP_ROLE__;

-- Tenant tables + partitions (partitions are tables; ALL TABLES covers knode_p*/arep_p*).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA __SCHEMA__ TO __APP_ROLE__;

-- Narrow the two GLOBAL tables back down:
--   ledger is read-only to the app (verify mode); only the owner-run migration writes it.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON __SCHEMA__.di_migration_ledger FROM __APP_ROLE__;
--   api keys are never hard-deleted (revocation = UPDATE disabled_at, di/auth.py).
REVOKE DELETE, TRUNCATE ON __SCHEMA__.di_api_key FROM __APP_ROLE__;

-- nextval() on doc_version_change_seq (005) and any future sequence.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA __SCHEMA__ TO __APP_ROLE__;

-- Future objects created by the migration role (new tables in 007+, new hash partitions
-- created programmatically by di/db.py) inherit the same runtime grants automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA __SCHEMA__
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO __APP_ROLE__;
ALTER DEFAULT PRIVILEGES IN SCHEMA __SCHEMA__
    GRANT USAGE, SELECT ON SEQUENCES TO __APP_ROLE__;
```

Convention to document in the file header of 006 and in README: **all migrations must run as di_owner** — the `ALTER DEFAULT PRIVILEGES` (which is per-creating-role) only covers objects di_owner creates. Future migrations that add global tables must REVOKE the excess themselves, mirroring the ledger/api_key lines.

## 3. di/db.py changes

- `run_migrations(settings)` (db.py:186): stop using the runtime pool (db.py:195-196). Open a dedicated `asyncpg.connect()` using migration credentials (`settings.pg_migration_user or settings.pg_user`, same fallback for password — empty settings preserve today's single-role behavior for bare local runs and unit tests). Apply `_init_conn` to it, keep the advisory lock (db.py:199-200,232-233), ledger, partition creation, and vector-column bootstrap exactly as-is, and close the connection at the end. Token rewrite at db.py:212 becomes: `raw.replace("__SCHEMA__", f'\"{settings.pg_schema}\"').replace("__APP_ROLE__", quoted(settings.pg_user))` where `quoted()` doubles embedded quotes. Checksum stays computed over the raw file (db.py:208), matching the existing __SCHEMA__ precedent.
- New `async def verify_migrations(settings)`: via the **runtime** pool, `SELECT filename, checksum FROM di_migration_ledger` and compare against `sorted(_MIGRATIONS_DIR.glob("*.sql"))` checksums; raise `RuntimeError` naming every pending/mismatched file. (di_app has SELECT on the ledger per 006.)
- New `async def assert_rls_posture(settings)` — the fail-closed guard, run on the runtime pool:
  1. `SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user` — both must be false.
  2. `settings.rls_enabled` must be true.
  3. For the 10 tenant tables (di_documents, doc_version, di_entity, client_merged_fact, di_decision_trace, knode, arep, di_job, di_blob, di_fact_adjudication): `SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relname = ANY($2) AND NOT (c.relrowsecurity AND c.relforcerowsecurity)` must return zero rows, and `SELECT count(*) FROM pg_policies WHERE schemaname=$1 AND tablename = ANY($2) AND policyname='tenant_isolation'` must equal 10.
  4. Warn (not fail) if `current_user` owns any tenant table (`pg_class.relowner`): FORCE still filters an owner, but least privilege is violated.
  Decision logic lives in a pure function `evaluate_rls_posture(env, rls_enabled, rolsuper, rolbypassrls, unforced_tables, policy_count) -> list[str]` so it is unit-testable without a DB; violations raise in `is_production` (config.py:121-123), log at WARNING otherwise (keeps `RLS_ENABLED=false` usable for pure-local hacking).

### GUC hygiene verification (no code change required; document in acquire()'s docstring)

`acquire()` (db.py:83-98) is already sound; state why explicitly: (a) `set_config('app.current_client_id', $1, false)` is **session-scoped** (db.py:92), which is safe only because the checkout resets it — and it does, twice: the explicit `finally` reset to `''` (db.py:98) which matches no real client_id, plus asyncpg's default pool release running `Connection.reset()` (`RESET ALL`, `UNLISTEN *`, `pg_advisory_unlock_all()`), which clears custom GUCs — this is the same mechanism that already forces the defensive `_init_conn` re-run on every checkout (db.py:89), since RESET ALL also clears search_path. (b) If the task is cancelled mid-checkout the `finally` still runs; if the connection itself died, the pool discards it and the GUC dies with the session. (c) `set_config(..., false)` inside an aborted transaction would roll back — irrelevant here because acquire() sets it before any transaction begins. (d) If a code path forgets `client_id` (acquire(None)), FORCE RLS makes `current_setting(..., true)` return NULL/'' → reads return zero rows and writes fail WITH CHECK (SQLSTATE 42501) — fail-closed, never fail-open. Global-table access (`acquire(None)` in di/auth.py:168-303 and storage/postgres.py:146) keeps working because di_api_key/di_migration_ledger have no RLS (005:153-154) and 006 grants exactly the verbs auth.py uses.

## 4. di/config.py additions

```python
# --- Migrations / DB roles ---
pg_migration_user: str = ""        # owner role for DDL; empty = fall back to pg_user (local/unit-test)
pg_migration_password: str = ""
migrations_mode: str = "auto"      # auto | verify | off  (validator like blob_backend, config.py:94-100)
```
Prod posture (documented + enforced by the guard extension): `di_env=prod|staging` requires `migrations_mode != "auto"` **or** explicit `pg_migration_user` distinct from `pg_user`; recommend `verify` so app pods never carry owner credentials.

## 5. di/app.py changes

- `_startup()` (app.py:75): after the pool connects (app.py:79-80), branch on `settings.migrations_mode`: `auto` → `run_migrations` (current app.py:105); `verify` → `verify_migrations`; `off` → skip. Then call `assert_rls_posture`.
- Fail-closed: in `is_production`, exceptions from migrations-verify or the posture guard must **propagate out of `lifespan`** (app.py:148-153) so uvicorn exits non-zero — replacing the degrade-and-continue behavior at app.py:107-109 for these two components only (readiness degradation remains for retrieval/blob/ocr). Record the failure in READINESS first so the crash log and /readyz agree.
- New file `di/migrate.py` with `if __name__ == "__main__"`: load settings, `asyncio.run(run_migrations())`, log applied files, exit non-zero on failure. This is the CI/CD / one-shot-job entrypoint: `docker run <image> python -m di.migrate`.

## 6. docker-compose.yml changes (demo runs the real posture)

- `db` service: mount `./docker/initdb:/docker-entrypoint-initdb.d:ro`. New file `docker/initdb/01_roles.sql`:
```sql
CREATE ROLE di_owner LOGIN PASSWORD 'di_owner' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE ROLE di_app   LOGIN PASSWORD 'di_app'   NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
GRANT CREATE, CONNECT ON DATABASE document_intelligence TO di_owner;
```
  (`POSTGRES_USER: di` remains only as the initdb bootstrap superuser; nothing connects as it afterwards.)
- `app` service env (docker-compose.yml:32-39): `PG_USER: di_app`, `PG_PASSWORD: di_app`, `PG_MIGRATION_USER: di_owner`, `PG_MIGRATION_PASSWORD: di_owner`, `RLS_ENABLED: "true"`, `MIGRATIONS_MODE: auto`. Zero-friction is preserved: still one `docker compose up --build`; migrations run in-app as di_owner under the advisory lock; traffic is served as di_app with FORCE RLS live.
- README + compose header comment: existing `pgdata` volumes predate the init script → one-time `docker compose down -v` (initdb scripts only run on an empty data dir). The app fails fast with a clear asyncpg auth error otherwise.

## 7. Failure modes handled

1. Prod boot as superuser / BYPASSRLS / RLS off / missing FORCE or policy on any tenant table → refused boot with a message naming the violated invariant (guard §3/§5).
2. Schema drift (image ships migration files the DB hasn't applied, or a mutated applied file — checksum mismatch, db.py:206-224) → verify mode refuses boot listing the files.
3. Forgotten GUC → zero rows read / 42501 on write (fail-closed; §3 GUC hygiene).
4. Owner credentials absent in prod app env → expected (verify mode); `auto` without them fails loudly at migration connect.
5. Rolling deploy, N replicas in `auto` → advisory lock serializes (db.py:200); replicas in `verify` → all check the ledger, first-deployed new image fails until the migration job runs (correct, fail-closed ordering).
6. Embedding-dim change in verify mode → vector columns (db.py:252-274) are only added by the migration step; document that a dim change requires re-running `python -m di.migrate`.
7. Grants drift on future objects → covered by ALTER DEFAULT PRIVILEGES as long as migrations run as di_owner (documented convention in 006 header).

## 8. Test plan

**Unit (no DB, joins the existing 365):** `evaluate_rls_posture` decision matrix (env × rls_enabled × rolsuper × rolbypassrls × unforced/missing-policy sets); migrations_mode + pg_migration_* validators; token rewrite test that `__APP_ROLE__` becomes a correctly quoted identifier (incl. a role name containing a quote); verify_migrations diff logic against fabricated ledger rows.

**Integration (`DI_RUN_INTEGRATION=1`, follows tests/conftest.py:51-85 gating), new tests/test_rls_isolation.py — the proof:** run against a DB provisioned with both roles (compose db service). (1) Seed tenant A and tenant B rows in ALL 10 tenant tables via `acquire('A')`/`acquire('B')`. (2) As di_app with `acquire('A')`: parametrized over the 10 tables, `SELECT count(*) WHERE client_id='B'` == 0 and unfiltered `SELECT count(*)` sees only A's rows; an owner/superuser control connection asserts B's rows really exist (proves filtering, not absence). (3) Write path: `acquire('A')` INSERT with `client_id='B'` into di_documents and di_job → expect SQLSTATE 42501 (`asyncpg.InsufficientPrivilegeError`); UPDATE/DELETE targeting B's rows → command tag affects 0 rows. (4) Unbound: `acquire(None)` SELECT on a tenant table → 0 rows. (5) GUC leakage: checkout as A, release, checkout again with no client_id from a min_size=1/max_size=1 pool → 0 rows (proves finally-reset + pool reset). (6) Posture: `pg_roles` shows current_user has neither rolsuper nor rolbypassrls; grants: DELETE on di_api_key and INSERT on di_migration_ledger as di_app → 42501. (7) verify mode: delete a ledger row as owner → `verify_migrations` raises.

**Smoke (tools/smoke_test.py):** add checks — /readyz reports migrations+rls components ok; create two client-scoped API keys via admin router, key A reading `/api/v1/clients/B/facts` → 403 (auth layer), and a wildcard-key ingest for A followed by tree read for B → empty (RLS+store layer). CI job: fresh `docker compose up` (clean volume) → smoke suite green, proving the demo path exercises RLS end-to-end.

## 9. Ops runbook (docs/ addition)

- **Password rotation:** roles use scram-sha-256 (`password_encryption=scram-sha-256`); rotate di_app by `ALTER ROLE di_app PASSWORD '...'` then rolling-restart with the new secret (two-phase: since Postgres allows only one password per role, for zero-downtime use paired roles di_app_a/di_app_b with identical GRANTs — 006's token design supports this by redeploying with PG_USER switched — or accept the brief reconnect window; pool retries cover it). Passwords injected from the platform secret manager, never in images or compose files for prod.
- **pg_hba/TLS:** prod pg_hba.conf only `hostssl document_intelligence di_app <app-cidr> scram-sha-256` and `hostssl document_intelligence di_owner <ci-cidr> scram-sha-256`; no `host` (non-TLS) lines, no `trust`. Client side: asyncpg `ssl` context with `verify-full` against the corporate CA (small addition to init_pool wiring, settings `pg_ssl_root_cert`).
- **Owner hygiene:** `ALTER ROLE di_owner NOLOGIN` outside migration windows (CI toggles LOGIN, runs `python -m di.migrate`, toggles back); enable `log_connections` and pgaudit `ddl` class so every owner session and DDL statement is attributable.
- **Monitoring:** alert on any pg_roles change (rolsuper/rolbypassrls), on policy count != 10 for tenant tables, and on app boots refused by the posture guard.

### Code touchpoints
- `di/migrations/006_grants.sql (new)`
- `docker/initdb/01_roles.sql (new)`
- `di/migrate.py (new __main__ entrypoint)`
- `tests/test_rls_isolation.py (new, integration-marked)`
- `di/db.py:186-233 (run_migrations → dedicated owner connection, __APP_ROLE__ token rewrite at :212)`
- `di/db.py:83-98 (acquire — docstring hardening notes only; behavior verified sound)`
- `di/db.py (new verify_migrations, assert_rls_posture, evaluate_rls_posture)`
- `di/config.py:36 (rls_enabled context) + new pg_migration_user/pg_migration_password/migrations_mode with validator like :94-100`
- `di/app.py:75-109 (_startup: migrations_mode branch, posture guard, hard-fail in is_production instead of degrade at :107-109)`
- `docker-compose.yml:13-23 (db initdb mount), :32-39 (PG_USER=di_app, PG_MIGRATION_USER=di_owner, RLS_ENABLED=true, MIGRATIONS_MODE=auto)`
- `tools/smoke_test.py (cross-tenant + posture checks)`
- `README / docs (down -v note, migration-step runbook, pg_hba/TLS/rotation)`

### Risks
- Existing demo volumes: initdb scripts only run on an empty pgdata volume, so every existing checkout needs a one-time `docker compose down -v`; without it the app fails to authenticate as di_app (mitigated by a loud README/compose note and a clear asyncpg error).
- Reliance on asyncpg's pool release reset (RESET ALL) as the second line of GUC defense — if the pool is ever created with reset disabled or a custom reset, only the finally-clause reset remains; the min_size=1 leakage integration test pins this behavior against asyncpg upgrades.
- ALTER DEFAULT PRIVILEGES binds to the creating role: if a future migration or hotfix is ever applied as a different role than di_owner, new tables silently lack runtime grants → runtime 42501s; mitigated by the documented convention and by verify-mode boot checks catching out-of-band DDL only partially (a grants-audit query in assert_rls_posture is a cheap follow-up).
- RLS predicate overhead on hot queries: current_setting() is evaluated per-row unless the planner hoists it; all tenant indexes already lead with client_id (002_core_tables.sql:30-31,46-48,59,76,90; 003:41-68) so plans stay index-scoped, but p99 should be benchmarked before/after on knode search paths.
- verify mode freezes runtime schema evolution: embedding-dim discovery (di/app.py:87-102) can no longer add vector columns at boot — a dim change silently no-ops until the migration step is re-run (documented, but an operator can miss it).
- The 006 CREATE ROLE fallback creates a NOLOGIN role that a DBA must activate; if a team mistakes it for a working setup they get connection failures — the error message must point at the runbook.

### Adversarial review — corrections (normative)

**Factual errors found in the proposal:**
- Design §1 claims 001_extensions.sql:8-9 creates 'the trusted extensions ltree/pgcrypto/vector' — 001 creates only ltree and pgcrypto (001_extensions.sql:8-9); vector is created at runtime by _try_enable_pgvector at di/db.py:244 inside run_migrations, with failures swallowed at db.py:245-246
- Ops runbook claims paired-role rotation is supported 'by redeploying with PG_USER switched — 006's token design supports this': false — run_migrations computes the checksum over the raw file (di/db.py:208) and skips 006 when it matches the ledger (di/db.py:209), so a PG_USER change never re-runs the grants and the new role has no privileges
- GUC-hygiene section attributes the defensive _init_conn re-run at db.py:89 to asyncpg's RESET ALL clearing search_path — the code's stated reason is 'pre-warmed conns may predate discovery' (pgvector schema discovery, di/db.py:89 comment); the RESET ALL interaction is real but is not what that line documents
- Minor line-cite drift: PG_USER: di is docker-compose.yml:34 (the design cites 35-39 for the superuser connection); the doc_version index cite '002:46-48' is 47-48; auth.py SELECT cites 188/264/303 are the statements spanning 186-189/263-267/302-304 — all substantively correct
- Verified correct (for the record): 10 tenant tables = 7 in 004_rls.sql:13-16 + 3 in 005_hardening.sql:159-161; FORCE at 004:20 and 005:165; GUC bind di/db.py:91-92 with double reset db.py:96-98 + asyncpg pool RESET ALL; superuser-di + RLS_ENABLED false in compose (13, 39); degrade-not-abort app.py:81-84,107-109; advisory lock db.py:199-233; no DELETE on di_api_key anywhere in di/auth.py (soft revoke at 213); DML inventory matches store.py/jobs.py/storage/postgres.py; DI_RUN_INTEGRATION gating in tests/conftest.py; 365 collected tests

**Design flaws to fix:**
- Cross-tenant read hole: 006 grants di_app DML on ALL TABLES including knode_p*/arep_p* partitions, but RLS policies exist only on the parents (004_rls.sql:18-27); Postgres applies only the named table's policies, so direct SELECT on a partition as di_app bypasses tenant isolation entirely, and ALTER DEFAULT PRIVILEGES re-opens the hole on every future partition created by di/db.py:132-139
- Key-rotation runbook is broken by the design's own ledger: redeploying with PG_USER switched to di_app_b never re-applies 006 because run_migrations skips files whose raw-file checksum matches (di/db.py:208-209) and the checksum is independent of the __APP_ROLE__ rewrite — the new role gets no grants and no default-privilege coverage; grants must target a fixed NOLOGIN group role with login roles as members
- No path for existing databases: every object in an already-bootstrapped DB is owned by the old single role, so 006's GRANTs, _create_hash_partitions (must own parent, di/db.py:132-139), and _ensure_vector_columns ALTER TABLE (di/db.py:266-267) all fail as di_owner; only the compose demo ('down -v') is handled — a superuser-run REASSIGN OWNED bootstrap step is required and unmentioned
- Fail-closed gap: the design hardens migrations/posture failures but leaves app.py:81-84 returning on DB-connect failure; the process stays alive and acquire() lazily rebuilds the pool (di/db.py:87), so a prod instance can end up serving traffic with the posture guard never having executed
- Contradicts the durable PG-queue-workers upgrade: a cross-tenant FOR UPDATE SKIP LOCKED claim on di_job returns zero rows under FORCE RLS with a single-tenant GUC, and the guard's 'policy count == 10, exactly tenant_isolation' invariant refuses boot when a legitimate role-targeted worker_claim policy is added — the worker role/policy must be reserved in this design
- verify mode blind spots: partition-count drift (_assert_partition_count only runs inside run_migrations, di/db.py:164-183) and embedding-dim mismatch (python -m di.migrate never performs the retrieval /api/models discovery that lives in app.py:87-102, so vector columns get embedding_dim_default) pass boot silently and fail at ingest time
- Privilege hazard in 001: CREATE EXTENSION ... SCHEMA public as non-superuser di_owner on PG15+/16 can be refused (no CREATE on public; initdb grants only DATABASE CREATE), and the runtime vector-create failure is swallowed (di/db.py:245-246), silently degrading to FTS-only on a pgvector image
- Blanket ALTER DEFAULT PRIVILEGES grants full DML on every future table — including append-only audit or auth-hardening tables — guarded only by a comment convention; the grants-audit the design defers as 'cheap follow-up' is the fail-closed control and belongs in assert_rls_posture now
- Migration runner crash window (pre-existing, but the design reuses it 'unchanged' while its docstring db.py:190-192 promises non-idempotent migrations): file apply (db.py:214) and ledger INSERT (db.py:218-224) are separate non-transactional statements, so a crash between them re-applies the file on next boot
- /readyz integrity: REQUIRED_COMPONENTS is ('db','migrations') (di/observability.py:45); without adding the new rls component, a failed posture guard in warn paths still reports ready=true, contradicting the smoke-test assertion the design specifies

**Missing pieces:**
- Superuser-run ownership-transfer step (REASSIGN OWNED BY <old_role> TO di_owner) for every pre-existing database — the only migration path covered is the compose demo's down -v
- Reserved worker role / di_job claim policy (or a relaxed guard invariant) for the announced durable PG-queue-workers upgrade
- Embedding-dim awareness in di/migrate.py (retrieval /api/models query or explicit dim flag) plus an atttypmod-vs-expected-dim check in verify/posture
- Partition-count check (_assert_partition_count) in the verify-mode boot path
- 'rls' added to REQUIRED_COMPONENTS in di/observability.py:45 so the smoke test's /readyz assertion can actually hold
- Grants-audit query in assert_rls_posture (design defers it to a follow-up despite its own fail-closed rationale)
- How tests/conftest.py obtains the owner/control DSN for the test_rls_isolation.py control connection (the harness currently knows one set of PG_* settings)
- Policy-predicate verification (pg_policies.qual) — the count-based guard accepts a USING(true) policy named tenant_isolation
- pg_ssl_root_cert / TLS settings appear in the runbook but not in the §4 config additions they depend on
- Behavior of MIGRATIONS_MODE=off (listed in §4 but never specified: no verify, no guard? presumably guard still runs — say so)

**Corrected design deltas:**
1. CLOSE THE PARTITION HOLE (blocking). 006's `GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA` plus `ALTER DEFAULT PRIVILEGES ... ON TABLES` gives di_app direct DML on knode_p*/arep_p*. RLS policies exist only on the parents (004_rls.sql:18-27 loops over 'knode','arep', never partitions), and Postgres applies only the policies of the table *named in the query* — so `SELECT * FROM knode_p3` as di_app returns every tenant's rows. Postgres checks privileges only on the named table too, so parent-level grants are sufficient for all app queries (store.py always addresses the parents). Fix: grant DML on an explicit table list (the 10 tenant tables + di_api_key/di_migration_ledger with their narrowing REVOKEs) instead of ALL TABLES; drop the blanket default-privilege for tables, or REVOKE from each partition inside `_create_hash_partitions` (di/db.py:132-139) right after creation; add "di_app has zero privileges on any partition" (`has_table_privilege` over pg_inherits children) to `assert_rls_posture`; add a direct-partition SELECT to test_rls_isolation.py.

2. GRANT TO A FIXED GROUP ROLE, NOT `__APP_ROLE__`. Replace the token with a constant NOLOGIN role (e.g. `di_app_rw`): 006 grants to it, and login roles (di_app, di_app_a/di_app_b for rotation) are made members out-of-band/initdb. This fixes the design's own broken rotation runbook — redeploying with PG_USER switched can NEVER re-grant, because run_migrations skips 006 when the ledger checksum matches and the checksum is computed over the raw file (di/db.py:208-209), independent of PG_USER — and it eliminates the identifier-vs-string-literal quoting problem (the token appears inside `to_regrole('__APP_ROLE__')` AND as a bare identifier in the same DO block, which one quoting function cannot serve), the CREATE ROLE TOCTOU across concurrent schemas, and the "edit-forbidden migration frozen to one role name" trap.

3. ADD AN OWNERSHIP-TRANSFER BOOTSTRAP FOR EXISTING DATABASES. Every object in any already-bootstrapped DB is owned by the old single role (`di` in compose). Applied as di_owner: 006's GRANTs fail (not owner/no grant option), `_create_hash_partitions` fails (must own parent), `_ensure_vector_columns` ALTER TABLE fails (di/db.py:266-267). `down -v` covers only the compose demo. Document and script a one-time superuser step (`REASSIGN OWNED BY <old_role> TO di_owner` in the database, or per-object ALTER ... OWNER TO) that must run before the new image boots against any existing DB; it cannot be a ledgered migration because di_owner cannot run it.

4. CLOSE THE db-unreachable BOOT GAP. The design hard-fails migrations-verify and posture-guard in prod but leaves app.py:81-84 intact: if the DB is down at startup, `_startup` returns, the process stays alive, and `acquire()` lazily re-creates the pool (di/db.py:87) later — serving traffic with the posture guard never having run. In `is_production`, DB-connect failure must also propagate out of lifespan, or the guard must be re-run on first successful lazy pool init.

5. RESOLVE THE QUEUE-WORKER CONTRADICTION NOW. A durable PG-based queue worker needs a cross-tenant `SELECT ... FROM di_job WHERE status='queued' FOR UPDATE SKIP LOCKED` with no tenant GUC; under FORCE RLS + tenant_isolation that returns zero rows for di_app, and the guard's `policy count == 10 with policyname='tenant_isolation'` invariant refuses boot the day a legitimate second policy is added. Either reserve a `di_worker` role with a role-targeted claim policy (`CREATE POLICY worker_claim ON di_job FOR SELECT/UPDATE TO di_worker USING (true)`) in this design, or change the guard predicate to "tenant_isolation exists on all 10" rather than "exactly one policy per table".

6. FIX verify-MODE BLIND SPOTS. (a) `python -m di.migrate` never learns the live embedding dim (discovery lives in app.py:87-102), so it creates vector(embedding_dim_default) columns; the app in verify mode then discovers a different live dim and cannot fix it — ingest fails per-row. Have migrate.py optionally query retrieval /api/models or take an explicit dim, and have verify/posture compare embedding-column atttypmod against the expected dim. (b) `_assert_partition_count` (di/db.py:164-183) runs only inside run_migrations; add the partition-count check to the verify/posture path so PG_HASH_PARTITIONS drift refuses boot in verify mode too.

7. EXTENSIONS/SCHEMA-public PRIVILEGE. 001 runs `CREATE EXTENSION ... SCHEMA public` (001_extensions.sql:8-9 — note: ltree and pgcrypto only; `vector` is created at runtime by `_try_enable_pgvector`, di/db.py:244, not in 001 as the design claims). On PG15+ non-superusers have no CREATE on schema public, and initdb grants only CREATE ON DATABASE; trusted-extension install into an explicit schema can still be refused, and the vector failure is swallowed silently (di/db.py:245-246) — degrading to FTS-only with no error on a pgvector image. Either `ALTER DATABASE document_intelligence OWNER TO di_owner` in initdb (cleanest: implies CREATE on db and, via pg_database_owner, on public) or add `GRANT CREATE ON SCHEMA public TO di_owner`; and make a pgvector-expected-but-absent condition visible in the posture/readiness output.

8. PROMOTE THE GRANTS AUDIT INTO SCOPE. The blanket `ALTER DEFAULT PRIVILEGES ... GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES` silently hands full DML on every future table — including the append-only/audit tables the compliance roadmap and auth-hardening upgrade imply — relying on a comment-level convention to REVOKE. The design already names the grants-audit query as a "cheap follow-up"; it belongs in assert_rls_posture in this change, per the design's own fail-closed principle. (Narrower alternative after correction 1: drop table default-privileges entirely and grant explicitly per migration.)

9. Smaller deltas: add the new "rls" component to REQUIRED_COMPONENTS (di/observability.py:45 is currently ("db","migrations")) or /readyz reports ready with the guard failed; wrap each migration file apply + ledger INSERT (db.py:214 and 218-224) in one transaction while touching this code (the runner docstring at db.py:190-192 promises future non-idempotent migrations, and the current crash window between apply and record would re-apply them); optionally check pg_policies.qual matches the expected predicate, since a `USING (true)` policy named tenant_isolation passes the count check; specify how tests/conftest obtains the second (owner) DSN for the control connection in test_rls_isolation.py; move `pg_ssl_root_cert` from the runbook prose into the §4 config additions.

---

## 4. Auth hardening: lifecycle, quotas, access audit, posture

**Effort:** L · **Reviewer verdict:** needs_changes (corrections below are normative)

### Current gap
Production auth posture is entirely convention, not enforcement. (1) Nothing stops di_env=prod from running with auth_enabled=false (di/auth.py:330-331 returns a wildcard principal), rls_enabled=false, mask_by_default=false, or the documented demo bootstrap key di_local_dev_key_change_me (docker-compose.yml:44) — startup never crashes on misconfiguration, it only records READINESS (di/app.py:75-139). (2) Keys never expire and there is no rotation flow — di_api_key has created_at/last_used_at/disabled_at but no expires_at (005_hardening.sql:30-39); revocation is the only lifecycle event (admin.py:139-148). (3) No rate limiting or per-tenant ingest quotas anywhere — a single leaked ingest key can saturate the pipeline. (4) 'Who read this client's data?' is unanswerable: there is no read-side audit at all; only mutations leave traces. (5) All secrets come from flat env (di/config.py:16-18) with no file-mount path for cloud secret managers. (6) The console persists the raw API key in localStorage indefinitely (frontend/src/lib/api.ts:126). (7) Bonus defect found while reading: read routers authenticate but never enforce the 'read' scope (clients.py:87-166, jobs.py:32, search.py:45, nodes.py:34 use bare require_principal), so an ingest-only key can read unmasked-eligible PII endpoints.

### Options considered

**A. Gateway-first minimal hardening** — Add only the startup posture guards, key expiry/rotation, and the read-scope fix. Delegate rate limiting, quotas, and access audit entirely to the bank's API gateway (Apigee/Cloud Armor) and SIEM (gateway access logs).

- ✅ Smallest diff; ships in days
- ✅ No new hot-path code in the app; zero added write load on Postgres
- ✅ Gateways genuinely are the right place for precise, global rate limits
- ✅ No new tables to operate
- ❌ Access audit via gateway logs cannot answer 'which tenant's data' — client_id is in path params the gateway does not model, and masked-vs-unmasked is invisible to it; the EA finding stays open
- ❌ Fails open when deployed without the assumed gateway (compose demo, internal direct calls, misrouted traffic)
- ❌ Per-tenant ingest fairness (GAP 1 tie-in) cannot be expressed at a gateway that does not know tenants
- ❌ Compliance evidence lives outside the platform's own trust boundary — harder to attest

**B. In-platform hardening on Postgres (no new infra)** — Crash-at-boot posture assertions; expires_at + rotate endpoint; in-process token buckets as a per-replica backstop plus Postgres-counted per-tenant ingest quotas; partitioned append-only di_access_log written by a batched async writer and dual-emitted as structured logs; *_FILE secret sourcing; sessionStorage-default console. Precise global rate limits still documented as the gateway's job.

- ✅ Every requirement answered inside the platform's own trust boundary — auditable and attestable by the bank
- ✅ Zero new infrastructure: Postgres (already there) + in-process state; compose demo path unchanged
- ✅ Quota checks ride existing indexes (di_job_client_status, di_job_client_created — 005:58-63); audit writes are batched so read-path latency is unaffected
- ✅ Fail-closed by construction: misconfigured prod refuses to boot rather than serving open
- ❌ In-process token buckets are per-replica (N replicas ⇒ N× nominal limit) — coarse, must be documented
- ❌ di_access_log grows fast at scale (~2-3 GB/day at 200 rps tenant reads); needs partition-drop/archive operations
- ❌ ~15-20 files touched; the largest of the three options to review

**C. Centralized auth infrastructure** — Redis for distributed exact rate limiting, IdP-minted short-lived JWTs replacing raw keys for the console, HMAC pepper via Cloud KMS/Vault, OPA sidecar for policy.

- ✅ Exact global rate limits across replicas
- ✅ Short-lived tokens eliminate long-lived key storage in browsers
- ✅ Pepper defends the key table against insert-a-known-hash tampering
- ❌ Redis + IdP + KMS are three new operational dependencies for a platform whose demo must run via docker compose
- ❌ Pepper adds nothing real: keys are random 256-bit, so sha256 preimage/brute-force is already infeasible; the pepper only matters against an attacker with DB write access, who can already tamper with facts — wrong layer to defend
- ❌ JWT minting for an internal console is overbuilt absent a bank IdP integration requirement
- ❌ Violates 'boring, operable, avoid infrastructure that does not pay for itself'

### Recommendation
Option B. A bank cannot outsource its compliance evidence to a gateway it merely hopes is in front of the service (Option A fails open, and gateway logs cannot attribute reads to tenants or masked/unmasked projections), and Option C buys exactness the platform does not need at the cost of three new dependencies. Option B keeps the trust boundary self-contained: posture violations crash the process before it serves a byte; key lifecycle, tenant quotas, and read audit all live in the Postgres the platform already operates, using indexes that already exist; the only thing deliberately left to the bank's gateway is precise global rate limiting and mTLS — both genuinely network-layer concerns. On the pepper question specifically: with 256-bit random keys (di/auth.py:115), sha256-without-pepper is already unbrute-forceable from a leaked table; an HMAC pepper defends only against an attacker who can INSERT into di_api_key, and that attacker can already corrupt merged facts — so the migration (rewrite every hash, dual-lookup window, second secret to rotate) is not worth it. Skip it and say why in the ADR. Everything remains compose-runnable: guards key off di_env=local, audit and buckets run in-process against the existing db container.

### Design (as proposed)
## GAP 4 design — Auth hardening for production

### 0. Decisions up front
- **Crash vs refuse-ready**: static config posture violations ⇒ CRASH (raise before the app serves). Rationale: refuse-ready only stops LB traffic; the socket still answers direct/internal calls, which is exactly the fail-open we are eliminating. Runtime-observed violations that need a DB connection (connected role is superuser/BYPASSRLS in prod) ⇒ also crash, but only on a *positively observed* violation — a merely unreachable DB keeps the existing refuse-ready path (di/app.py:81-84) so transient outages don't crash-loop.
- **Pepper**: rejected (see recommendation). Record as ADR in docs.
- **Rate limiting**: in-process token bucket per key as backstop (documented per-replica); precise global limiting is the gateway's job. No Postgres counters on the read hot path.
- **Ingest quotas**: Postgres-counted at submit time (ingest is low-QPS relative to reads; one indexed count is cheap) — this is the fairness hook GAP 1's queue consumes.
- **Access audit**: append-only, monthly-partitioned di_access_log + batched async writer + dual-emit structured log line (SIEM path survives a DB blip). No sampling for tenant-data reads — sampling PII-access audit is a compliance non-starter; instead we bound volume by logging only tenant-scoped routes and by partition retention/archival.

### 1. Migration — di/migrations/006_auth_hardening.sql
Follows the __SCHEMA__ + idempotency conventions of 005 (di/db.py:206-224 replaces the token and ledgers the checksum).

```sql
-- 006_auth_hardening.sql — key lifecycle, tenant policy, read-side access audit. Idempotent.

-- Key lifecycle: expiry + rotation lineage + per-key rate override.
ALTER TABLE __SCHEMA__.di_api_key
    ADD COLUMN IF NOT EXISTS expires_at     timestamptz,
    ADD COLUMN IF NOT EXISTS rotated_from   uuid,
    ADD COLUMN IF NOT EXISTS rate_limit_rps integer,
    ADD COLUMN IF NOT EXISTS created_by     text;

-- Per-tenant operational policy (quotas). GLOBAL like di_api_key (005:28-30): read at admission,
-- before any tenant GUC is bound, and administered cross-tenant — so no RLS policy.
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_tenant_policy (
    client_id          text PRIMARY KEY,
    max_active_jobs    integer,          -- NULL = settings default
    daily_ingest_limit integer,          -- NULL = settings default; 0 = blocked
    note               text,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Read-side access audit. Append-only, RANGE-partitioned by month on ts.
-- GLOBAL (no RLS): rows are written in batches spanning tenants on one connection, and read
-- only via admin scope — a tenant GUC filter would break the writer (cf. 005:153-155 rationale).
CREATE TABLE IF NOT EXISTS __SCHEMA__.di_access_log (
    id         bigint GENERATED ALWAYS AS IDENTITY,
    ts         timestamptz NOT NULL DEFAULT now(),
    key_id     text,
    principal  text,
    client_id  text,
    method     text NOT NULL,
    route      text NOT NULL,           -- route TEMPLATE, e.g. /api/v1/clients/{client_id}/facts
    status     smallint NOT NULL,
    masked     boolean,                 -- serving projection: was the masked view returned?
    request_id text,
    extra      jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (ts, id)                -- partition key must be in the PK
) PARTITION BY RANGE (ts);

CREATE INDEX IF NOT EXISTS di_access_log_client_ts ON __SCHEMA__.di_access_log (client_id, ts DESC);
CREATE INDEX IF NOT EXISTS di_access_log_key_ts    ON __SCHEMA__.di_access_log (key_id, ts DESC);

-- Append-only enforcement: block UPDATE/DELETE at the trigger level (partition DROP/DETACH is
-- DDL and unaffected, so retention still works). CREATE OR REPLACE + DROP/CREATE TRIGGER = idempotent.
CREATE OR REPLACE FUNCTION __SCHEMA__.di_access_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'di_access_log is append-only';
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS di_access_log_no_rewrite ON __SCHEMA__.di_access_log;
CREATE TRIGGER di_access_log_no_rewrite
    BEFORE UPDATE OR DELETE ON __SCHEMA__.di_access_log
    FOR EACH ROW EXECUTE FUNCTION __SCHEMA__.di_access_log_immutable();

-- Bootstrap current + next month partitions so first writes never race partition creation.
DO $$
DECLARE m date;
BEGIN
    FOR i IN 0..1 LOOP
        m := date_trunc('month', now())::date + (i || ' month')::interval;
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS __SCHEMA__.%I PARTITION OF __SCHEMA__.di_access_log '
            'FOR VALUES FROM (%L) TO (%L);',
            'di_access_log_' || to_char(m, 'YYYY_MM'), m, m + interval '1 month');
    END LOOP;
END$$;
```
(Note: inside the DO block __SCHEMA__ is substituted textually by di/db.py:212 before execution, same as 005's DO block at 005:156-174.)

### 2. Production posture guard — new di/posture.py
`assert_production_posture(settings) -> None`, called as the FIRST statement of `create_app()` (di/app.py:156-158, before FastAPI construction). When `settings.is_production` (di/config.py:122-123), raise `RuntimeError` listing ALL violations at once (not first-failure) if any of:
- `auth_enabled` is False (di/config.py:50)
- `mask_by_default` is False (di/config.py:59)
- `rls_enabled` is False (di/config.py:36)
- `di_bootstrap_api_key == "di_local_dev_key_change_me"` (the compose/smoke-test demo value, docker-compose.yml:44) — or is non-empty but shorter than 32 chars. Empty is ALLOWED and preferred in prod: first key comes from the CLI (below).
- `access_audit_enabled` is False (new setting).
The raise happens before `lifespan`, so uvicorn exits nonzero ⇒ CrashLoopBackoff ⇒ visible. A second, DB-side check runs in `_startup()` (di/app.py:75) right after the pool opens: in production, `SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user`; if either is true, log CRITICAL and `raise SystemExit(3)` — RLS (004_rls.sql) is theater under a bypassing role, and this is a positively observed violation, not a transient fault. Add a `posture` component to READINESS and extend `REQUIRED_COMPONENTS` to `("db", "migrations", "posture")` (di/observability.py:45); non-prod sets it ok=True with detail "non-production: guards inactive" so /readyz documents the posture either way.

### 3. Key lifecycle — di/auth.py + di/routers/admin.py + new di/tools/keys.py
- `_PUBLIC_COLS` (di/auth.py:54) gains `expires_at, rotated_from, rate_limit_rps`.
- `resolve_principal` (di/auth.py:239-275): add `AND (expires_at IS NULL OR expires_at > now())` to the lookup; cache entry TTL becomes `min(CACHE_TTL_SECONDS, seconds until expires_at)` so an expiring key dies on time despite the memo. `Principal` gains `rate_limit_rps: int | None`.
- `create_api_key` (di/auth.py:151-175): new kwargs `expires_at: datetime | None`, `created_by: str | None`, `rotated_from: str | None`, `rate_limit_rps: int | None`; INSERT extended accordingly. Admin `ApiKeyRequest` (admin.py:46-49) gains optional `expires_at` and `rate_limit_rps`; `created_by` is stamped from the calling principal's name.
- **Rotation endpoint**: `POST /api/v1/admin/keys/{key_id}/rotate` (admin.py). Body: `{"overlap_hours": int = settings.key_rotation_overlap_hours}` (default 24, max 168). In one transaction: read the old row (must be live and unexpired); INSERT a new key with identical client_ids/scopes/rate_limit_rps, `name = old.name + "@" + YYYYMMDD`, `rotated_from = old.id`; UPDATE old row `expires_at = LEAST(coalesce(expires_at,'infinity'), now() + overlap)`. Evict old key's cache entries (same loop as revoke, auth.py:219-221). Response: new `ApiKeyCreated` + `old_key_expires_at`. Flow is exactly create-new → overlap-window → old key auto-dies; `revoke_api_key` remains the immediate kill switch, and `last_used_at` (005:37) tells the operator when the old key has actually gone quiet before the window ends.
- **CLI** `python -m di.tools.keys create --name ... --scopes read --client-ids acme --expires-in-days 90` (new file di/tools/keys.py, ~80 lines, reuses auth.create_api_key over the normal pool): the production first-key path that replaces the bootstrap env var, so prod never needs a long-lived wildcard secret in its environment. Compose keeps the bootstrap env path (di_env=local, guard inactive).
- **Multi-replica cache note**: `revoke`/`rotate` evict only the local replica's cache; other replicas converge within CACHE_TTL_SECONDS=30 (di/auth.py:47). Document 30 s as the revocation SLA; do not build cross-replica invalidation for this.

### 4. Read-scope enforcement (defect fix)
Replace `Depends(require_principal)` with `Depends(require_scope("read"))` in di/routers/clients.py:87,102,117,138,151,166, jobs.py:32,49, search.py:45, nodes.py:34. Wildcard-scope keys (bootstrap, local-dev principal auth.py:123-130) are unaffected; any narrowly-scoped ingest key loses read access it should never have had. Called out in CHANGELOG as a deliberate breaking tightening.

### 5. Rate limiting + tenant ingest quotas — new di/ratelimit.py
- `TokenBucket` (pure, unit-testable): capacity `burst`, refill `rps`, monotonic clock injected.
- Module-level `dict[key_id, TokenBucket]`, pruned lazily (drop buckets idle > 10 min). Enforcement inside `require_principal` (di/auth.py:316-338) after resolution, when `settings.rate_limit_enabled`: rps = `principal.rate_limit_rps or settings.rate_limit_default_rps`; on exhaustion raise 429 with `Retry-After` and increment a `di_rate_limited_total{key}` counter in di/observability.py. Local-dev principal is exempt. Documented loudly: the bucket is PER REPLICA — N replicas admit up to N×rps; this is a backstop against runaway/leaked keys, and exact global limits belong to the bank's gateway.
- **Ingest quota** in di/routers/ingest.py, after `authorize_client` (ingest.py:63) and before `create_job` (ingest.py:85): one query — `SELECT count(*) FILTER (WHERE status IN ('queued','running')) AS active, count(*) FILTER (WHERE created_at >= date_trunc('day', now())) AS today FROM di_job WHERE client_id=$1` — rides existing indexes di_job_client_status / di_job_client_created (005:58-63). Limits from di_tenant_policy row (cached in-process 60 s) else `settings.ingest_max_active_jobs_per_client` (default 25) / `settings.ingest_daily_limit_per_client` (default 0 = unlimited). Breach ⇒ 429 `{"detail": "tenant ingest quota exceeded", "quota": ...}` + Retry-After. The check is admission-time, so it composes with GAP 1's queue (fairness at admission; the queue never sees an over-quota tenant's job). Race window between count and insert across replicas is accepted: quotas are fairness bounds, not invariants. Admin CRUD: `GET/PUT /api/v1/admin/tenants/{client_id}/policy` in admin.py (admin scope, `authorize_client` applied).

### 6. Read-side access audit — new di/audit.py + middleware in di/app.py
- `AccessLogWriter`: `asyncio.Queue(maxsize=settings.access_audit_queue_max=10_000)`; background task started in lifespan (di/app.py:147-153), flushed every `access_audit_flush_ms=1000` or `access_audit_batch=500` rows via one `INSERT ... SELECT * FROM unnest($1::..., ...)` on `acquire(None)`. `_shutdown()` (di/app.py:142-144) drains the queue BEFORE `close_pool()`. On insert error mentioning a missing partition, create next month's partition (same format-string as the migration) and retry once.
- **Capture point**: one HTTP middleware registered in `create_app()`. After `await call_next(request)`, the scope is routed, so it reads `scope["route"].path` (template, no PII in the logged route) and `scope["path_params"].get("client_id")`; handlers that learn client_id from the body (ingest, form param ingest.py:55) set `request.state.audit_client_id`, and `require_principal` stashes `request.state.principal`. Only requests with a resolved tenant client_id are recorded (health/metrics/console assets are not). `masked` is stamped by the serving endpoints via `request.state.audit_masked` (di/serving.py projections already know it). Dual-emit: the same record goes to `logging.getLogger("di.access").info(json.dumps(...))` so the SIEM path is independent of the DB row.
- **Failure mode / strictness**: `access_audit_strict` (prod default true, local false). Queue full ⇒ strict: request fails 503 `{"detail": "audit unavailable"}` (fail-closed: no un-audited PII reads); non-strict: drop + `di_audit_dropped_total` counter. Writer-task DB failures set READINESS component `audit` unhealthy (visible on /readyz) and, in strict mode, trip the same 503 path once the queue saturates.
- **Volume & retention (weighed)**: row ≈ 150-200 B; at a sustained 200 rps of tenant reads ⇒ ~17 M rows/day ≈ 2.5-3.5 GB/day with both indexes ⇒ ~90-100 GB/month partition. Postgres handles this fine as append-only monthly partitions, but unbounded retention does not scale to years — so: partitions older than `access_audit_retention_days` (default 400) are DETACHed, exported (CSV/parquet via new CLI `python -m di.tools.audit_export --month 2026-06 --dest <object store>`), then dropped; the bank's long-horizon retention (7 y KYC) lives in object storage, not Postgres. No sampling of tenant-data reads, ever; volume control comes from scope (tenant routes only) + retention, not from gaps in the record. Startup ensures current+next partitions exist (helper in di/audit.py called from `_startup`).
- Query surface: `GET /api/v1/admin/access-log?client_id=&from=&to=&cursor=` (admin scope, keyset pagination on (ts,id) mirroring di/jobs.py) — answers the EA finding "who accessed this client's data" directly.

### 7. Secrets sourcing — di/config.py
Add a `_FILE` indirection: a customized settings source (pydantic-settings `settings_customise_sources`) that, for every settings field, checks `<ENV_NAME>_FILE`; if set, the value is `Path(...).read_text().strip()`. Applies to `pg_password`, `di_bootstrap_api_key`, `retrieval_api_key`, `azure_vision_key`, `s3_secret_key` for free. This keeps code provider-neutral while mapping cleanly to: GCP Secret Manager → Cloud Run `--set-secrets DI_BOOTSTRAP_API_KEY=di-bootstrap-key:latest` (env injection — the team's existing convention) or volume mounts (`PG_PASSWORD_FILE=/secrets/pg-password`); Kubernetes secret volumes; Vault agent sink files. Rule stated in README: compose may carry demo values; any non-local env must source secrets via env-from-secret-manager or `_FILE` — never literals in YAML.

### 8. Console key handling — frontend/src
localStorage is not acceptable as the silent default even for an internal tool (persistent raw credential, exfiltratable by any XSS, survives shared-workstation sessions). Proportionate fix, no token service: (a) default storage becomes `sessionStorage` (per-tab, gone on close); (b) an explicit "remember key on this device" checkbox opts into localStorage for the local demo — change `useLocalStorage.ts` to accept a storage backend and `useSettings.tsx:41` / `lib/api.ts:125-133` to consult a `di.apiKey.persist` flag; migrate/remove any pre-existing `di.apiKey` localStorage value on load unless the flag is set; (c) update the hint text in `components/States.tsx:150`; (d) add a `Content-Security-Policy: default-src 'self'; ...` header when serving the SPA (di/app.py `_mount_frontend`, app.py:44-72) to shrink the XSS surface that makes browser-stored keys risky; (e) ops guidance: console operators use read-scope, 90-day-expiry keys (now expressible via expires_at). Short-lived minted tokens are explicitly deferred until a bank IdP/SSO integration exists — building a token service in front of a key box adds a secret exchange without removing the key.

### 9. mTLS / service-mesh positioning (documented, not built)
The platform terminates plain HTTP and owns application-layer identity (X-API-KEY → Principal → tenant grants → RLS). Transport identity — mTLS between gateway and app, client-cert auth, WAF, exact global rate limits, IP allow-listing — belongs to the bank's edge (gateway/mesh; on GCP: internal ingress + IAM invoker + Cloud Armor per existing team conventions). The platform's obligations at that seam: never trust X-Forwarded-* except from the configured proxy hop, keep /metrics and /readyz off the public route map at the gateway, and keep functioning WITHOUT the gateway present (fail-closed via its own auth) — which is exactly what the posture guard guarantees. Recorded as an ADR so nobody later builds cert handling into uvicorn.

### 10. Config additions (di/config.py)
```
access_audit_enabled: bool = True
access_audit_strict: bool = False          # posture guard forces consideration in prod; recommend true
access_audit_queue_max: int = 10000
access_audit_batch: int = 500
access_audit_flush_ms: int = 1000
access_audit_retention_days: int = 400
rate_limit_enabled: bool = True
rate_limit_default_rps: int = 50
rate_limit_burst: int = 100
ingest_max_active_jobs_per_client: int = 25
ingest_daily_limit_per_client: int = 0     # 0 = unlimited
key_rotation_overlap_hours: int = 24
```

### 11. docker-compose.yml changes
Under app.environment: add `ACCESS_AUDIT_ENABLED: "true"`, `ACCESS_AUDIT_STRICT: "false"`, `RATE_LIMIT_ENABLED: "true"`, `RATE_LIMIT_DEFAULT_RPS: "1000"` (never trips in demo), `INGEST_MAX_ACTIVE_JOBS_PER_CLIENT: "25"`; extend the comment at docker-compose.yml:41-44: "DI_ENV=local disables the production posture guard; with DI_ENV=staging|prod this compose file will refuse to boot (demo bootstrap key + RLS_ENABLED=false), which is intentional." Demo path otherwise unchanged; tools/smoke_test.py gains checks (rotate flow, 429 on quota breach, access-log row present after a read).

### 12. Rollout
1. Land 006 migration + code; deploy — migration applies under the advisory lock (di/db.py:196-233); existing keys have NULL expires_at (no behavior change).
2. Fix read-scope on read routers (announce: keys lacking `read` lose read access).
3. Enable audit non-strict in staging; watch di_access_log growth + di_audit_dropped_total for a week; then strict in prod.
4. Rotate the real environments' keys via the new endpoint; set expires_at on all newly minted keys (90-day standard); revoke the old bootstrap key and unset DI_BOOTSTRAP_API_KEY in prod (CLI becomes the first-key path).
5. Flip staging/prod DI_ENV and verify the posture guard by deliberately mis-setting one flag in staging (expect crash-loop) before trusting it.

### 13. Test plan
Unit (pure, no DB, matching existing style): posture matrix (every violating flag alone + combined + local bypass + message lists all violations); TokenBucket refill/burst/exhaustion with injected clock; audit record shaping + queue-overflow strict/non-strict; rotation arithmetic (overlap clamps, LEAST with pre-existing expires_at); Principal expiry-aware cache TTL. Integration (DI_RUN_INTEGRATION=1): 006 applies twice idempotently (checksum ledger short-circuit di/db.py:209-211); expired key gets 401 within TTL bound; rotate → both keys valid during overlap → old 401 after; ingest 429 at max_active_jobs then admits after a job finishes; read with ingest-only key now 403; access-log row appears with correct route template/client_id/masked after GET facts; UPDATE/DELETE on di_access_log raises; partition rollover (insert with ts in next month succeeds). Smoke (tools/smoke_test.py): +6 checks per rollout item above. Frontend: vitest for storage-backend selection + persist-flag migration.

### Code touchpoints
- `di/migrations/006_auth_hardening.sql (new)`
- `di/posture.py (new)`
- `di/ratelimit.py (new)`
- `di/audit.py (new)`
- `di/tools/keys.py (new)`
- `di/tools/audit_export.py (new)`
- `di/auth.py:54 (_PUBLIC_COLS + new cols)`
- `di/auth.py:151-175 (create_api_key kwargs)`
- `di/auth.py:239-275 (resolve_principal expiry + expiry-aware cache)`
- `di/auth.py:316-338 (require_principal: rate-limit hook + request.state.principal)`
- `di/routers/admin.py:120-148 (rotate endpoint, tenant-policy CRUD, access-log query, expires_at on create)`
- `di/routers/ingest.py:63-90 (quota admission check + request.state.audit_client_id)`
- `di/routers/clients.py:87,102,117,138,151,166 (require_scope('read'))`
- `di/routers/jobs.py:32,49 (require_scope('read'))`
- `di/routers/search.py:45 (require_scope('read'))`
- `di/routers/nodes.py:34 (require_scope('read'))`
- `di/app.py:156 (assert_production_posture in create_app)`
- `di/app.py:75-144 (DB-role posture check, audit writer start/drain, CSP header in _mount_frontend, audit middleware)`
- `di/observability.py:45 (REQUIRED_COMPONENTS += 'posture'; new counters)`
- `di/config.py:15-133 (new settings + _FILE secret source)`
- `di/serving.py (stamp request.state.audit_masked at projection call sites)`
- `docker-compose.yml:28-69 (new env vars + posture comment)`
- `frontend/src/hooks/useLocalStorage.ts (storage backend param)`
- `frontend/src/hooks/useSettings.tsx:41 (sessionStorage default + persist flag)`
- `frontend/src/lib/api.ts:125-133 (storage selection)`
- `frontend/src/components/States.tsx:150 (hint text)`
- `tools/smoke_test.py (new checks)`

### Risks
- Read-scope tightening is a behavioral break: any existing key minted with only ['ingest'] silently loses read endpoints — must be announced and keys re-minted before the deploy that includes it.
- access_audit_strict=true couples the read path to audit-writer health: a sustained Postgres write stall converts to 503s on reads. Mitigated by the bounded queue absorbing bursts and /readyz surfacing the 'audit' component, but the bank must accept 'no audit ⇒ no reads' explicitly; staging soak in non-strict mode first.
- In-process token buckets multiply by replica count; if the bank scales to many replicas without a gateway limiter in front, effective limits drift upward — the documentation must make the gateway dependency for exact limits unmissable.
- di_access_log at high read QPS needs the archival/drop runbook to actually run; if it doesn't, the table grows ~100 GB/month and autovacuum/backup times degrade. The export CLI exists but scheduling it is an ops obligation, not code.
- Posture guard turns previously 'working' misconfigured prod deployments into crash loops on upgrade day — intended, but rollout step 5 (deliberate staging failure test) must precede prod to avoid a surprise outage window.
- Ingest quota check-then-insert races across replicas can overshoot max_active_jobs by roughly the replica count under concurrent submits; acceptable for fairness bounds, but if GAP 1 later needs hard caps, the count must move into the same transaction as create_job.
- Middleware reads scope['route'] after call_next — safe on current Starlette, but a Starlette major upgrade could change scope population timing; pin/verify in the upgrade checklist.

### Adversarial review — corrections (normative)

**Factual errors found in the proposal:**
- The audit-capture design assumes client_id is always a path param plus two body cases: it is a QUERY parameter on the jobs routes (di/routers/jobs.py:28 `client_id: str` on GET /api/v1/jobs, jobs.py:49 on GET /api/v1/jobs/{job_id}) and on node provenance (di/routers/nodes.py:33 `client_id: str` on GET /api/v1/nodes/{node_id}/provenance). `scope["path_params"].get("client_id")` returns None for all three, so as designed these tenant-data reads (job error messages, document names, node provenance with attribute keys and bboxes) silently escape di_access_log — the exact 'who read this client's data' hole the design claims to close.
- Touchpoint 'frontend/src/lib/api.ts:125-133 (storage selection)' is off: lines 125-126 are the API_KEY_STORAGE constant; the localStorage-reading default getter is api.ts:137-143. The substantive claim (raw key persisted in localStorage) is correct — persistence happens via useLocalStorage in useSettings.tsx:41 — but the edit site cited is the wrong lines.
- 'Quota checks ride existing indexes (di_job_client_status, di_job_client_created — 005:58-63)' is wrong for the query as written. A single `SELECT count(*) FILTER (...active...), count(*) FILTER (...today...) FROM di_job WHERE client_id=$1` has no selective predicate beyond client_id, so the planner does ONE scan of every job row for the tenant (index prefix on client_id at best) — it cannot use di_job_client_status for one aggregate and di_job_client_created for the other. For a tenant with years of jobs this is a full per-tenant scan on every submit. Two scalar subqueries (one per index) actually ride the indexes.

**Design flaws to fix:**
- Streaming ingest bypasses the quota twice: `?stream=true` (di/routers/ingest.py:68-76) runs the pipeline inline and never calls create_job, so (a) if the quota check is placed 'before create_job (ingest.py:85)' as written, stream requests skip the check, and (b) even if checked, streaming ingests create no di_job row, so they are invisible to the active-jobs count. A leaked ingest key saturates the pipeline via stream=true exactly as before — the headline threat the quota exists for.
- The quota query's RLS binding is unspecified, and getting it wrong fails silent-open: di_job is ENABLE+FORCE RLS with the tenant_isolation policy (005_hardening.sql:156-174), so running the count on `acquire(None)` under the prod non-superuser role returns zero rows — every count is 0 and the quota NEVER trips, precisely in the rls_enabled=true production posture the guard mandates. It works in the compose demo (RLS_ENABLED=false, docker-compose.yml:39), so tests pass and prod is unprotected. Must be `acquire(client_id)`.
- Runtime partition DDL contradicts the RLS role-split upgrade: the audit.py startup helper, the insert-retry `CREATE TABLE ... PARTITION OF`, and the audit_export DETACH/DROP all require ownership of di_access_log / CREATE on the schema — rights the role split deliberately strips from the lean runtime role. Under that upgrade the retry path fails, the writer's queue backs up, and in strict mode all reads 503. Partition creation must live under the migration/owner role (e.g. inside run_migrations' advisory-locked path per boot), and 006 must GRANT INSERT on di_access_log and SELECT/INSERT/UPDATE on di_tenant_policy to the runtime role.
- The missing-partition retry is wrong at month rollover: 'create next month's partition and retry once' — on Sep 1 the failing insert needs the SEPTEMBER partition (created as 'next' back in July only if the replica restarted during August), but 'next month' from Sep 1 is October, so the retry creates October and the September insert fails again, forever, on any replica that ran >1 month without restart. The retry must derive the partition month from the failing rows' ts. Also concurrent CREATE TABLE ... PARTITION OF across N replicas can race (brief ACCESS EXCLUSIVE on the parent; duplicate pg_type errors even with IF NOT EXISTS) — serialize under the existing advisory lock (db.py:142-144 _lock_key).
- Internal contradiction on strict mode: §6 says access_audit_strict '(prod default true, local false)' but §10 defines `access_audit_strict: bool = False` and the §2 posture guard checks only access_audit_enabled. As specified, a default prod deploy runs non-strict audit that silently DROPS records on queue overflow — which the design itself calls 'a compliance non-starter'. Either the posture guard must require strict in prod or the default must be env-dependent.
- Crash-window audit loss: up to access_audit_queue_max=10,000 already-served reads (or one flush window) vanish on SIGKILL/OOM — _shutdown drains only on graceful stop (app.py:142-144). The dual-emit log line is the only mitigation, and the design never says WHEN it is emitted; if the writer task emits it, it dies with the queue. The structured log must be emitted synchronously in the middleware before the response is returned, making stdout the crash-safe record and Postgres the queryable one.
- Strict-mode readiness coupling is inverted: when the audit writer is unhealthy, strict mode 503s every tenant read on that replica, but 'audit' is not in REQUIRED_COMPONENTS (only db, migrations, posture) — /readyz stays 200 and the LB keeps routing traffic into guaranteed 503s. In strict mode, audit-unhealthy must flip readiness so the replica drains. (Note READINESS today is written only at startup; the writer task setting it at runtime is new behavior — fine, but say so.)
- Invalid-key requests are unthrottled DB load: the token bucket keys on the RESOLVED principal, and only successful resolutions are cached (auth.py:88-90, 269-270), so every invalid-key request is one uncacheable SHA-256 lookup against di_api_key. A credential-stuffing or junk-key flood bypasses the rate limiter entirely and lands on Postgres. Needs a per-IP/global failed-auth bucket or a short negative cache.
- Rotate race: the read-old-row → INSERT new → UPDATE old transaction has no SELECT ... FOR UPDATE, so two concurrent rotations of the same key both see it live and mint TWO successor keys with rotated_from pointing at the same parent. Lock the row (or guard with a partial unique index on rotated_from WHERE disabled_at IS NULL).
- CSP `default-src 'self'` will likely break the console it protects: inline style attributes (React style={} props, theme color-scheme set at useSettings.tsx:72) require style-src 'unsafe-inline'; the header must be derived from the actual Vite bundle and verified against the built SPA, not written from a template — otherwise rollout step 3 discovers a blank console in staging.
- `raise SystemExit(3)` inside lifespan startup is fragile: SystemExit is a BaseException with special asyncio/uvicorn handling; the reliable, already-used pattern is a normal exception propagating out of lifespan startup ('Application startup failed. Exiting.'). Use RuntimeError like the static guard.
- GET /api/v1/admin/access-log applies admin scope but (unlike the tenant-policy CRUD in §5) no authorize_client: an admin-scoped key restricted to client_ids=['acme'] can read access logs for every tenant, or omit client_id and dump the global log. Apply authorize_client when client_id is given and require the wildcard tenant grant for unfiltered queries.
- GAP-1 (durable PG queue) coupling is fragile in one spot: the quota's status list ('queued','running') is a hardcoded copy of today's JobStatus (di/jobs.py:57-61); if the queue upgrade adds leased/retrying states, active-job counting silently undercounts. Derive the non-terminal set from JobStatus/_TERMINAL (jobs.py:65) instead of literals.

**Missing pieces:**
- GRANT statements in 006 for the new GLOBAL tables (di_access_log, di_tenant_policy) and a stated owner/executor role for all runtime partition DDL and the audit_export DETACH/DROP — mandatory once the RLS role-split upgrade lands.
- Audit coverage policy for denied requests: 401/403 attempts against tenant routes are compliance-relevant ('who TRIED to read this client's data'); the design logs whatever has a resolved client_id but never states this as a requirement or tests it, and the middleware must catch-and-reraise unhandled exceptions to record 500-status reads (the audit middleware sits inside ServerErrorMiddleware, so an exception propagates through call_next as a raise, not a response).
- Scheduling for retention: the export CLI exists but nothing runs it — no cron/K8s CronJob spec, no alert on oldest-partition age or di_access_log total size; the design's own risk list admits this is the failure mode, so ship the alert with the feature, not as a runbook hope.
- Bootstrap-key interaction with rotation: ensure_bootstrap_key re-runs every startup (app.py:130-136); state explicitly that ON CONFLICT DO NOTHING (auth.py:297-299) preserves a rotated/expiring bootstrap row and that the posture guard plus unsetting DI_BOOTSTRAP_API_KEY is the only thing preventing prod from resurrecting a wildcard key — and add a smoke/integration test for 'rotated bootstrap key stays expiring across restart'.
- CLI first-key path bootstrap ordering: di.tools.keys create against a fresh prod DB before the first app boot finds no schema/di_api_key table; the CLI must either run run_migrations first or fail with an actionable message.
- Multi-replica TokenBucket state with multiple uvicorn workers per pod: buckets are per-PROCESS, so N pods x W workers multiplies the effective limit further than the documented 'per replica'; the doc must say per-process or pin workers=1.
- Console persist-flag migration test: removing a pre-existing di.apiKey localStorage value on load unless the persist flag is set is a destructive migration for every existing operator session — needs an explicit UX note (operators will be logged out once) in the rollout plan.
- The frontend purge call (api.ts:322-327, DELETE /api/v1/clients/{client_id}) does not match any backend route (backend has POST /api/v1/admin/clients/{client_id}/purge, admin.py:80-96) — pre-existing defect adjacent to this gap's console work; worth folding into the same frontend PR since read-scope tightening already touches these files.

**Corrected design deltas:**
1) Audit capture: resolve tenant as path_params.get('client_id') → request.query_params.get('client_id') → request.state.audit_client_id, and add an integration test asserting GET /api/v1/jobs and GET /api/v1/nodes/{id}/provenance produce di_access_log rows. 2) Quota: run the counts on acquire(client_id) (di_job is FORCE RLS, 005:163-165); split into two scalar subqueries so each uses its index; place the check immediately after authorize_client (ingest.py:63) BEFORE the stream branch, and either count in-flight streaming ingests via an in-process counter folded into max_active_jobs or gate stream=true behind the same quota with a documented per-replica caveat; derive the active-status set from JobStatus minus _TERMINAL (jobs.py:65), not string literals. 3) Partitions: create/ensure partitions inside run_migrations' advisory-locked startup path under the owning role (compatible with the RLS role split), add GRANTs for di_access_log and di_tenant_policy to 006, and fix the insert-retry to create the partition for the failing row's ts month, not "next month". 4) Audit durability/strictness: emit the structured di.access log line synchronously in the middleware before returning the response (crash-safe record); resolve the §6/§10 contradiction by making the posture guard require access_audit_strict=true in prod (or an explicit documented opt-out env), and add 'audit' to REQUIRED_COMPONENTS when strict so a failing writer drains the replica instead of serving 503s behind a green /readyz. 5) Auth hot path: add a failed-auth backstop (global/per-IP token bucket or 5s negative cache on unknown key hashes) so invalid keys cannot hammer di_api_key. 6) Rotate: SELECT ... FOR UPDATE on the old row inside the transaction; optionally a partial unique index on rotated_from WHERE disabled_at IS NULL. 7) Access-log admin endpoint: apply authorize_client on the client_id filter; require wildcard tenant grant for unfiltered reads. 8) Posture: use RuntimeError (not SystemExit) for the DB-role check; document that di_env=dev escapes all guards (is_production covers only staging/prod/production, config.py:122-123) and state that dev must not hold real tenant data. 9) CSP: derive the policy from the actual built bundle (expect style-src 'unsafe-inline' at minimum) and verify the console renders under it in the smoke test before enabling. 10) Fix the api.ts touchpoint to 137-143 and stamp audit_masked in the routers (which already compute `masked`, clients.py:91,106; search.py:50) rather than serving.py — fewer call sites and no coupling to the projection rewrite the multi-valued-facts upgrade will do.

---

## Integration: conflicts, sequence, migrations

## Interactions

1. **Migration numbering collision (all four designs claim `006_*.sql`).** The runner applies `sorted(glob("*.sql"))` (di/db.py:207), so filenames are the ordering mechanism. Resolution: one global sequence — 006_roles_and_grants.sql (rls), 007_auth_hardening.sql (auth), 008_multi_valued_facts.sql (mvf part 1), 009_multi_valued_cutover.sql (mvf part 2), 010_job_queue.sql (queue), 011_doc_version_unique.sql (queue, deferred index). Details in Migration plan.

2. **Queue workers need cross-tenant `di_job` visibility vs the RLS role split.** The queue design's GUC-based `queue_worker_access` policy uses exactly the trust model the RLS design (and both verdicts) reject. Resolution: kill the GUC policy entirely. 006 creates a NOLOGIN group role `di_worker` and a role-targeted policy `worker_claim ON di_job TO di_worker USING (true) WITH CHECK (true)` — scoped to `di_job` only, so tenant isolation on every other table is untouched. This policy ships in phase 1, before the queue exists, so the posture guard and security review see the final shape once.

3. **Worker DB credentials × the role split.** Workers do two kinds of work: cross-tenant claim/reap/heartbeat on `di_job`, and tenant-scoped pipeline writes. Resolution: two group roles — `di_app_rw` (tenant DML, granted in 006) and `di_worker` (the di_job claim policy). Login roles: `di_app` (API, member of di_app_rw) and `di_worker_login` (worker, member of di_app_rw AND di_worker), created by initdb/DBA, never by migrations. The worker pool connects as `di_worker_login`; `acquire(client_id)` binds the tenant GUC exactly as the API does for pipeline writes; the claim query needs no GUC because the role-targeted policy is permissive (OR-combined). `acquire_worker()` and the `app.job_queue_worker` GUC are deleted from the queue design. LISTEN uses a dedicated non-pool connection as `di_worker_login`.

4. **Posture guard invariant vs the worker policy.** The RLS design's "exactly one policy named tenant_isolation per tenant table" refuses boot the day `worker_claim` lands. Resolution: guard checks (a) `tenant_isolation` exists on all 10 tenant tables with the expected `pg_policies.qual` predicate, (b) the only additional policy permitted is `worker_claim` on `di_job` targeted `TO di_worker`, (c) runtime roles are NOSUPERUSER/NOBYPASSRLS/non-owner, (d) di_app_rw has zero privileges on any `knode_p*`/`arep_p*` partition (the partition hole — revoked in code right after `_create_hash_partitions` creates each one).

5. **Three overlapping admission controls: queue backpressure (tenant/global queued caps), auth quotas (max_active_jobs / daily limit / di_tenant_policy), per-key rate limits.** These are the same control point specified twice plus a third layer. Resolution: ONE admission check in di/routers/ingest.py, ordered: `authorize_client` → idempotency pre-check (a retried already-accepted submit must never 429) → unified per-tenant quota (queued + active + daily, limits from di_tenant_policy falling back to settings) → blob-put → enqueue. The quota query runs under `acquire(client_id)` (di_job is FORCE RLS — `acquire(None)` returns zero rows and the quota never trips in prod), as two scalar subqueries riding di_job_client_status / di_job_client_created. `?stream=true` is gated by the same quota with an in-process inflight-stream counter folded into the active count. The GLOBAL queued cap is dropped in v1 (unimplementable under tenant RLS without SECURITY DEFINER); global protection = per-key token bucket in `require_principal` (per-process, documented) + worker-side depth alerts + the bank gateway for exact limits. Claim-side fairness ships as plain SKIP LOCKED FIFO + soft per-tenant running cap (per queue verdict correction 10); the window-function round-robin is deferred until measured.

6. **Quota status literals vs new JobStatus vocabulary.** Auth's quota hardcodes ('queued','running'); the queue adds 'dead'/'canceled'. Resolution: the active set is derived as `set(JobStatus) - _TERMINAL` (di/jobs.py:65) so the queue phase changes it automatically. Terminal taxonomy unified: attempts-exhausted always ends 'dead' (alertable), 'failed' reserved for non-retryable classification. 'canceled' ships WITH its producer (admin cancel endpoint, queued→canceled only) in the same phase as 'retry' — no dead vocabulary in the CHECK constraint.

7. **Queue idempotency leans on the `client_merged_fact` UNIQUE that multi-valued facts replaces.** Resolution: idempotency of re-executed merges comes from `replace_merged_facts` full-set replace inside one transaction under a per-client `pg_advisory_xact_lock`, not from any specific constraint — so the constraint change is safe provided multi-valued facts (with the lock and replace semantics) lands BEFORE the queue multiplies same-client parallelism. That fixes phase order: mvf (phase 3) before queue (phase 4).

8. **Concurrent remerge is destructive once stale-delete exists.** Both verdicts independently demand per-client serialization. Resolution: `pg_advisory_xact_lock(hashtextextended(schema || ':' || client_id, 0))` as the first statement of the replace transaction. One lock serves both designs. The mvf integration test asserts no-lost-rows convergence (final set equals serial re-merge), not merely absence of unique violations.

9. **instance_key changes the adjudication key the admin API and console use.** Resolution: `AdjudicationRequest.instance_key` (default ''), fail-closed validation that accepts an instance_key matching an existing merged row OR an existing adjudication row (fixes the reject-is-a-one-way-door hole), new DELETE clear-verdict endpoint that re-merges, new append-only `di_fact_adjudication_event` history table (tenant table → gets a tenant_isolation policy and di_app_rw grants in 008, following phase 1 conventions; stop resetting created_at on upsert). Frontend scope stated honestly: the console has no adjudication UI today — phase 3 ships row-key/grouping/instance-badge changes only; adjudication UI is a separate, optional work item.

10. **Queue at-least-once retry × the pipeline noop shortcut (silent data loss).** create_version commits before knodes/areps/merge; a crash in that window plus retry hits the content-hash noop and succeeds with zero knodes. Resolution (queue phase, migration 010): `ingest_complete boolean NOT NULL DEFAULT true` on doc_version (default true so pre-existing rows are treated complete), set false at create and flipped true in the same transaction as the last pipeline write; the noop check requires `is_current AND ingest_complete`; a retry of an incomplete version deletes that version's knodes/areps and rewrites them in one transaction. create_version returns (version_no, supersedes) decided under its advisory lock, and ltree base path / done event / supersedes_id are computed after it. All worker-side job writes (set_status, append_event, complete) are fenced on `locked_by` + attempt.

11. **Blob-at-accept × blob_backend posture.** Resolution: posture guard — in prod, async (202) ingest requires blob_backend in (postgres, s3); `none` returns 503 at accept and flags readiness; `local` is documented as single-node/compose-only (multi-node requires shared RWX or is disallowed). Blob writes/reads run under the tenant GUC (di_blob is a tenant table) — no interaction with the worker role.

12. **Blob lifecycle × right-to-erasure × the queue itself.** "Optional weekly GC" is not bank-acceptable. Resolution: blob GC is a scheduled, audited job kind on the same queue; dead-job payload blobs get a retention TTL; `purge_client` is extended to cancel queued/running jobs and purge job payload blobs. Pre-006 orphaned job rows (payload '{}') are claimed-and-failed with an explicit "payload lost pre-upgrade" error, excluded from poison-pill alerting.

13. **All four designs touch run_migrations; the runner needs shared hardening once.** Resolution (phase 0/1, one change): (a) wrap each file apply + ledger INSERT in one transaction; (b) refuse to boot if an applied file's checksum changed (drift guard — the ledger currently re-executes mutated files); (c) migrations run on a dedicated owner connection (`di_owner`), never the runtime pool; (d) MIGRATIONS_MODE auto|verify|off with `python -m di.migrate` as the CI entrypoint; (e) all runtime DDL — hash partitions, vector columns, access-log monthly partitions — moves under the owner connection. Documented constraint: the runner's single-script execution forbids CREATE INDEX CONCURRENTLY; large long-lived tables use the out-of-band CONCURRENTLY + IF NOT EXISTS no-op procedure.

14. **Access-log partition DDL × verify mode × role split.** The audit writer cannot create partitions (runtime role has no DDL) and "create next month and retry" is wrong at rollover anyway. Resolution: `di.migrate` (and auto-mode boot) pre-creates partitions N months ahead (default 3) under the advisory lock; readiness checks partition horizon ≥ 1 month and alerts; the writer never does DDL — on missing partition it dual-emits to stdout (which is emitted synchronously in middleware anyway, making stdout the crash-safe record) and marks the audit component unhealthy. Strict mode adds 'audit' to REQUIRED_COMPONENTS so a failing writer drains the replica instead of 503ing behind a green /readyz.

15. **Audit `masked` stamping × the serving projection rewrite.** mvf rewrites project_facts; auth wanted serving.py to stamp audit_masked. Resolution: stamp `request.state.audit_masked` in the routers (which already compute `masked`) — decouples the two designs. Audit tenant resolution: path_params → query_params → request.state.audit_client_id (covers the jobs/nodes query-param routes). Masking additionally strips `identity_basis` from resolution_rationale for HIGH-sensitivity rows (mvf leak), and `payload` is excluded from the Job API model (queue leak).

16. **REQUIRED_COMPONENTS accumulates across phases.** Resolution: one posture/readiness registry grown per phase — db, migrations (existing) + posture, rls (phase 1) + audit-when-strict (phase 2) + queue (phase 4). The API process also exports queue_stats gauges so queue depth is observable when zero workers are alive. DB-unreachable-at-boot in prod crashes (or re-runs the posture guard on lazy pool init) — closes the guard-never-ran gap.

17. **Embedded worker default × production posture.** Resolution: `ingest_embedded_worker` defaults to `not settings.is_production`; the posture guard refuses prod boots with it true. Compose runs the real topology (embedded off, dedicated worker container) so the demo exercises what prod ships.

18. **`__APP_ROLE__` token × key rotation × ledger checksums.** A PG_USER change never re-runs 006, so token-based grants break rotation. Resolution: no token; 006 grants only to the fixed group roles; rotation = paired login roles (di_app_a/di_app_b) swapped via membership, no migration involved. Bootstrap-key resurrection: prod unsets DI_BOOTSTRAP_API_KEY (guard enforces), `di.tools.keys` CLI runs migrations first or fails with an actionable message, and a test pins "rotated bootstrap key stays expiring across restart".

19. **Two sources of 429 (quota + backpressure) and new statuses/fields on existing responses.** Resolution: one documented 429 + Retry-After error shape in OpenAPI covering both; release notes explicitly flag (a) new JobStatus values breaking strict-enum clients, (b) multiple rows per attribute_key + FactsResponse.count semantics change, (c) read-scope tightening (ingest-only keys lose read), (d) ownership.* moving to HIGH sensitivity.

20. **One frontend PR bundles the collisions.** sessionStorage default + persist flag (auth), CSP derived from the actual Vite bundle and smoke-verified (expect style-src 'unsafe-inline'), Facts.tsx instance rendering + duplicate-React-key fix (mvf), and the pre-existing purge-endpoint mismatch (frontend calls DELETE /clients/{id}; backend is POST /admin/clients/{id}/purge).

## Sequence

**Phase 0 — Foundations and independent defect fixes (effort: S, ~3-4 days).**
Ships: migration-runner hardening (per-file transaction, checksum-drift boot refusal, sorted-glob ordering test); read-scope enforcement on all read routers (`require_scope("read")`); frontend purge-endpoint fix; `tools/check_doc_version_dupes.py`; `tools/bootstrap_roles.sql` (superuser one-time: create di_owner/di_app_rw/di_worker group roles + login roles + REASSIGN OWNED BY di TO di_owner).
Why first: everything downstream writes migrations through this runner; the read-scope defect is a live privilege hole independent of all designs; the bootstrap script is a prerequisite for phase 1 on any existing database.
Testable: unit tests for runner drift/transaction behavior; integration test that an ingest-only key gets 403 on read routes; bootstrap script applied to a copy of the demo volume.

**Phase 1 — RLS production posture (rls-production, effort: M, ~1.5-2 weeks).**
Ships: 006_roles_and_grants.sql (group-role grants on the explicit table list, PUBLIC revokes, ledger/api_key narrowing, sequence grants, `worker_claim` policy on di_job reserved now); partition REVOKE in `_create_hash_partitions`; owner-connection migrations + MIGRATIONS_MODE (auto/verify/off) + `python -m di.migrate`; posture guard (`evaluate_rls_posture` pure function + DB-side checks per interaction 4, including partition-privilege audit, policy-predicate check, embedding-dim and partition-count checks in verify mode); prod crash-on-DB-unreachable; docker/initdb roles script; compose flips to PG_USER=di_app, RLS_ENABLED=true; tests/test_rls_isolation.py (cross-tenant zero rows, WITH CHECK 42501, GUC leakage, partition direct-select denied, grants matrix).
Why this order: every later migration creates objects whose grant/RLS conventions this phase defines; retrofitting the role split after the queue/audit land would force re-migrations and a second security review (both verdicts said exactly this). The worker role/policy is reserved here so phase 4 needs no security-model change.
Testable/demoable: `docker compose up` (after `down -v` or bootstrap script) serves the full existing demo with RLS actually enforced; the isolation test suite is the headline auditor artifact; deliberately mis-set staging flag → crash-loop demo.

**Phase 2 — Auth hardening (auth-hardening, effort: L, ~2-3 weeks).**
Ships: 007_auth_hardening.sql; key expiry + rotation endpoint (FOR UPDATE on old row) + `di.tools.keys` CLI; failed-auth negative cache/backstop bucket; per-key token buckets (documented per-process); unified per-tenant ingest quota at the single admission point (interaction 5, with statuses derived per interaction 6 — quota is queue-ready before the queue exists); di_tenant_policy admin CRUD; access-audit middleware (synchronous stdout emit + batched Postgres writer, strict-mode posture and readiness wiring per interactions 14/16, authorize_client on the access-log admin endpoint, 401/403/500 coverage); retention export CLI + partition-horizon and oldest-partition alerts shipped with the feature; `_FILE` secret sourcing; posture guard extended (auth flags, bootstrap key, strict audit in prod); console key storage + CSP (frontend PR part 1).
Why here: admission control and audit must exist before the queue changes ingest mechanics, so the queue phase modifies one admission path rather than creating a competing one; key lifecycle is independent of the data-model work and de-risks the fleet early.
Testable/demoable: rotate flow end-to-end in smoke; 429 quota breach demo; "who read client X" answered from di_access_log after a console session; expired key 401 within TTL.

**Phase 3 — Multi-valued facts (effort: L, ~2-3 weeks, spans two releases).**
Release N ships: 008_multi_valued_facts.sql (instance_key columns, new unique indexes, di_fact_adjudication_event, old constraints KEPT); all code — fingerprinting with one normalization for identity AND within-instance conflict, `replace_merged_facts` (per-client advisory xact lock, full-set replace, empty-set-deletes-all), tuple-keyed adjudications, reversible reject + clear-verdict endpoint + adjudications list, instance_count over pre-filter rows, ownership.* → HIGH + rationale redaction, router-level audit_masked stamping, Facts.tsx rendering (frontend PR part 2) — behind `multi_valued_enabled=false` (multi_keys forced empty, so all writes are instance_key='' and satisfy the old constraint).
Release N+1 ships: 009_multi_valued_cutover.sql (drop old UNIQUE constraints, gated on the flag mechanism) and flips `multi_valued_enabled=true`; optional `tools/remerge_backfill.py`.
Why before the queue: interaction 7/8 — the per-client lock and replace semantics must exist before the queue multiplies same-client parallelism across replicas; and today's in-process runner already allows same-client concurrency, so the lock is needed regardless.
Testable/demoable: Acta Constitutiva 3-director E2E (3 rows, no false conflict, reject one survives re-ingest, clear-verdict restores it); no-lost-rows concurrent-remerge test; single-key golden regression byte-identical.

**Phase 4 — Durable queue + workers (effort: L-XL, ~3-4 weeks).**
Ships: 010_job_queue.sql; blob-first accept path folded into the phase-2 admission point (idempotency pre-check first); `di/worker.py` (claim/heartbeat/reaper, fenced set_status/append_event/complete per interaction 10, claim ≤ free semaphore slots, dedicated LISTEN connection, empty-payload special case, graceful drain); ingest_complete noop fix + authoritative version threading; arep job kind with per-version idempotency; retry + cancel admin endpoints; unified dead taxonomy; queue observability (API-side gauges too); blob GC job kind + purge_client extension (interaction 12); delete ingest_runner.py; compose worker service + `--scale worker=3`; embedded-worker default per interaction 17.
Why here: it consumes phase 1 (worker role/policy, owner-run DDL), phase 2 (admission point, posture registry), and phase 3 (remerge lock, replace semantics). It is the largest change and benefits from everything else being stable.
Testable/demoable: kill-a-worker-mid-OCR chaos demo (job completes elsewhere); fairness demo (1 job for tenant B completes while tenant A has thousands queued); kill-between-create_version-and-insert_knodes test proving the retry rebuilds rather than nooping; dead-letter → retry round-trip.

**Phase 5 — doc_version uniqueness + production cutover (effort: S, ~2-4 days + soak).**
Ships (one release after phase 4's create_version lock/retry code): 011_doc_version_unique.sql, gated by the dupes-check tool; out-of-band CONCURRENTLY procedure documented for large environments; final posture review; staging soak with strict audit + deliberate-failure drills; prod flag flips (MIGRATIONS_MODE=verify, embedded worker off, strict audit on).
Why last: old-code replicas must all carry UniqueViolation handling before the index can exist (both verdicts' two-release requirement).
Testable: concurrent same-doc different-content ingest yields distinct version_no; same-content noops; migration applies clean on the soaked staging DB.

Total: roughly 10-13 engineer-weeks; phases 2 and 3 can partially overlap after phase 1 (different subsystems, one shared frontend PR).

## Migration plan

New files, in apply order (runner sorts by filename; all idempotent, `__SCHEMA__`-tokenized, pure DDL — no DML, backfill is application re-merge):

- **006_roles_and_grants.sql** (phase 1): create NOLOGIN group roles di_app_rw/di_worker if absent (DO block swallowing duplicate_object); REVOKE ALL FROM PUBLIC on schema + tables; GRANT SELECT/INSERT/UPDATE/DELETE on the explicit list of the 10 tenant tables to di_app_rw; GRANT SELECT/INSERT/UPDATE on di_api_key (no DELETE), SELECT-only on di_migration_ledger; sequence USAGE; `worker_claim` policy on di_job TO di_worker; NO blanket ALL TABLES, NO ALTER DEFAULT PRIVILEGES for tables (each future migration grants its own objects), no `__APP_ROLE__` token. Partition revokes live in `_create_hash_partitions` code, not here.
- **007_auth_hardening.sql** (phase 2): di_api_key ADD expires_at/rotated_from/rate_limit_rps/created_by; di_tenant_policy (global, no RLS) + GRANT SELECT/INSERT/UPDATE to di_app_rw; di_access_log RANGE-partitioned + append-only trigger + GRANT INSERT/SELECT to di_app_rw + 3-months-ahead partition bootstrap (DO block); indexes.
- **008_multi_valued_facts.sql** (phase 3, release N): ADD instance_key DEFAULT '' to client_merged_fact and di_fact_adjudication; CREATE new unique indexes (client_id, attribute_key, instance_key) on both; di_fact_adjudication_event append-only history table + tenant_isolation policy + di_app_rw grants; old UNIQUE constraints are NOT dropped here.
- **009_multi_valued_cutover.sql** (phase 3, release N+1): DROP the two old auto-named UNIQUE constraints (IF EXISTS; names verified against a live pre-006 DB during implementation). Code flag multi_valued_enabled flips true only in this release.
- **010_job_queue.sql** (phase 4): di_job ADD kind/payload/priority/attempts/max_attempts/run_after/lease_expires_at/locked_by; partial claim + lease indexes; status CHECK including dead/canceled (NOT VALID); fillfactor/autovacuum settings; doc_version ADD ingest_complete boolean NOT NULL DEFAULT true. No RLS policy needed (worker_claim exists since 006). No doc_version unique index here.
- **011_doc_version_unique.sql** (phase 5, one release after 010's code): CREATE UNIQUE INDEX doc_version_client_doc_no (client_id, doc_id, version_no). Gated by tools/check_doc_version_dupes.py; large environments build it out-of-band with CONCURRENTLY first (IF NOT EXISTS makes the file a no-op).

**Fresh database:** 001→011 apply in one pass. Every referenced object exists before it is altered (di_job from 005 before 006's policy and 010's columns; di_api_key/di_fact_adjudication from 005 before 007/008; client_merged_fact/doc_version from 002 before 008/010/011). Role creation in 006 is idempotent and independent of initdb; login roles come from docker/initdb (compose) or the DBA. Hash partitions and vector columns are created by runner code after the files, under the owner connection, with partition revokes applied at creation — so 006 needs no forward references. Confirmed clean.

**Existing demo database:** prerequisite is one of (a) `docker compose down -v` (initdb re-provisions roles; recommended for the demo), or (b) run `tools/bootstrap_roles.sql` once as the existing `di` superuser (creates roles, REASSIGN OWNED BY di TO di_owner) — mandatory for any long-lived environment, since 006's grants, partition creation, and vector-column ALTERs all require di_owner ownership. After that, 006→011 apply cleanly: ledger rows for 001-005 have matching checksums (files are never edited; the phase-0 drift guard enforces it), all new DDL is additive, 008 keeps old constraints so old code survives release N, 009's drops are the only destructive step and are sequenced behind the flag, and 011 is gated by the dupes check (demo DB is small; check is instant). Pre-010 stuck 'running'/'queued' job rows are claimed and explicitly failed with "payload lost pre-upgrade" — not rescued.

**Runner-level guarantees shipped in phase 0/1 that the plan depends on:** per-file apply+ledger transaction; boot refusal on checksum drift; owner-connection execution; verify mode compares ledger to shipped files and refuses boot on mismatch; documented no-CONCURRENTLY constraint with the out-of-band procedure.

## Open questions for the owner

1. Zero-downtime requirement: is a brief merge-stage failure window during single-release cutovers acceptable anywhere, or must the two-release gates (008/009 and 010/011) be enforced in every environment including staging? This decides whether phases 3 and 5 can each collapse to one release for non-prod.
2. Is a bank API gateway (exact global rate limits, mTLS, WAF) actually committed in front of this service, and on what timeline? The per-process token buckets are designed as a backstop on that assumption.
3. Strict audit semantics: does the bank formally accept "no audit ⇒ no reads" (503s when the audit writer is unhealthy)? This is a business risk acceptance, not an engineering call.
4. Fingerprint inference channel: HMAC the instance_key with a permanent deployment-scoped secret (key ceremony required, rotation forbidden), or written risk acceptance of the unsalted hash? Compliance must choose.
5. Retention numbers: access-log retention horizon and archive destination (object store), dead-job payload blob TTL, and right-to-erasure SLA for queued/in-flight work — all need compliance-approved values before phase 2/4 configs are set.
6. Which attribute keys are multi-valued at launch (the proposed set: ownership.director, ownership.beneficial_owner, ownership.authorized_signer, account.number)? Ontology cardinality is a business/KYC decision, and promotion later requires the adjudication-migration runbook.
7. Are there any long-lived databases besides the compose demo today? If yes, the REASSIGN OWNED bootstrap and the doc_version dupes check need change-management scheduling; if no, the plan's existing-DB path is only exercised in staging.
8. Integrator communications: who owns notifying API consumers of the four breaking-ish changes (429/Retry-After, new job statuses, multiple rows per attribute_key, read-scope tightening), and what per-tenant burst sizes should the initial quota defaults reflect?
9. Is the 30-second key-revocation SLA (cache TTL convergence across replicas) acceptable, or is cross-replica invalidation required?
10. Does DI_ENV=dev ever hold real tenant data? The posture guard covers only staging/prod; if dev has real data, either extend the guard or ban real data in dev by policy.
