# Production cutover runbook — Phase 5

Covers the three things Phase 5 of `docs/specs/2026-07-15-enterprise-scale-plan.md` ships:
the `doc_version` uniqueness rollout, the final posture review, and the prod flag flips that
turn off every demo/dev convenience the compose stack relies on.

## 1. `doc_version` uniqueness rollout (migration 011)

`011_doc_version_unique.sql` adds `CREATE UNIQUE INDEX IF NOT EXISTS doc_version_client_doc_no
ON doc_version (client_id, doc_id, version_no)`. It ships one release after 010's
advisory-locked `store.create_version()` (with its unique-violation retry-once backstop) —
see the migration file's header for why the two are sequenced.

**Fresh or low-traffic databases** (a new environment, or one that has only ever run
010-and-later code): deploy normally. The migration runner applies the file directly in well
under a second — there is no concurrent-ingest history old enough to have produced a duplicate.

**Large, long-lived environments** (anything that ran the pre-010 pipeline, with no advisory
lock around version creation, for real traffic):

1. Check for existing duplicates first:
   ```
   python tools/check_doc_version_dupes.py
   ```
   If it reports duplicate groups, repair them (renumbers every duplicate but the earliest to a
   free `version_no`; no other table references `doc_version` by number, only by `id`, so this
   is safe):
   ```
   python tools/check_doc_version_dupes.py --repair --dry-run   # review the plan first
   python tools/check_doc_version_dupes.py --repair             # then apply it
   ```
   Re-run without `--repair` until it reports `OK: no ... duplicates`.

2. Build the index out-of-band, **outside the migration runner** (the runner wraps every file
   in a single transaction, and Postgres refuses `CONCURRENTLY` inside a transaction block — this
   step is why 011 is a separate release, not a code requirement of `011_doc_version_unique.sql`
   itself). Run this against the primary, as a role with index-build privileges on `doc_version`,
   outside of any open transaction — e.g. a bare `psql` session:
   ```sql
   CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS doc_version_client_doc_no
       ON di.doc_version (client_id, doc_id, version_no);
   ```
   (Replace the schema name if `PG_SCHEMA` is not `di`.) This holds no long-lived exclusive lock
   — concurrent reads and writes continue throughout the build.

3. Deploy the release containing `011_doc_version_unique.sql` as normal. Because the index
   already exists, the migration's `CREATE UNIQUE INDEX IF NOT EXISTS` statement is a fast
   catalog-only check — no further locking, no manual ledger row needed, the runner records it
   as applied like any other file.

## 2. Final posture review

`di.posture.evaluate_static_posture` is authoritative and runs automatically at boot for any
`DI_ENV` in `staging|prod|production` (`di.posture.assert_production_posture`, called before
`FastAPI()` is even constructed — a violation crashes the boot, it never serves degraded). This
section exists so the checks are legible without reading the source, and to record the
complement that boot-time cannot check (facts that need a live DB connection, or that are
policy rather than settings).

**Checked automatically at every production boot** (`di/posture.py`):
- `RLS_ENABLED=true`
- `MIGRATIONS_MODE=verify`, or a distinct `PG_MIGRATION_USER` if `auto` is kept
- `AUTH_ENABLED=true`
- `MASK_BY_DEFAULT=true`
- `DI_BOOTSTRAP_API_KEY` unset, or at minimum not the demo value and ≥32 chars
- `ACCESS_AUDIT_ENABLED=true` and `ACCESS_AUDIT_STRICT=true`
- `INGEST_EMBEDDED_WORKER=false`
- `BLOB_BACKEND` is `postgres` or `s3` (not `none`, not `local`)
- `INSTANCE_FINGERPRINT_HMAC_KEY` set

**Checked at runtime, needs a live DB connection** (`di.db.assert_rls_posture` — run this
against the actual production database before cutover, not just in CI against compose):
- The connecting role is neither `rolsuper` nor `rolbypassrls`
- Every tenant table's `tenant_isolation` RLS policy predicate actually references
  `current_client_id` (not a stale or dropped policy)
- No unexpected extra policy exists beyond the documented set (`tenant_isolation`,
  `worker_claim`) — an extra permissive policy silently widens access
- The runtime role holds no `SELECT` privilege on partition tables directly (only through the
  parent, which RLS filters) — a direct grant on a partition is an RLS bypass

**Not automatically checked — confirm manually before go-live:**
- `INSTANCE_FINGERPRINT_HMAC_KEY` is sourced from Secret Manager, not a literal env value in
  version control, and is backed up somewhere durable — rotating it orphans every existing
  multi-valued-fact adjudication keyed on the old fingerprints (no rotation support exists).
- The bootstrap API key (if any was used to mint the first real key) has been revoked via
  `python -m di.tools.keys` / `DELETE /api/v1/admin/keys/{id}` after real keys exist.
- `docker-compose.yml`'s demo-only settings (`DI_BOOTSTRAP_API_KEY`,
  `INGEST_MAX_ACTIVE_JOBS_PER_CLIENT=25` sized for a laptop demo, single `db`/`azure-ocr-mock`
  containers) are not what's deployed — production topology is the API Deployment, a separate
  `python -m di.worker` Deployment (see §3), a managed Postgres instance, and the real Azure
  Computer Vision endpoint.
- `BLOB_BACKEND=local` is never used across more than one node (§9 of the design: no shared RWX
  volume assumption holds outside compose) — `postgres` or `s3` only, for any multi-node
  deployment.
- Every worker replica and every API replica are running the SAME migration-ledgered code
  version — mixed pre-010/post-010 replicas mid-rollout is exactly the hazard §1 above and the
  migration 011 header address; do not let a rolling deploy span more than one release boundary
  for this pair.

## 3. Staging soak

Before flipping any of §4's flags in production, soak the same flags in staging for at least
one full business cycle (a day of real-shaped ingest traffic), with `ACCESS_AUDIT_STRICT=true`
already on (§4) so the soak proves that setting under load, not just in isolation.

Deliberate-failure drills to run during the soak (each one was live-verified once against the
local compose stack during Phase 4 development — repeat them here against staging's real
topology, not just the demo compose file):

1. **Kill a worker mid-job.** Scale workers down while jobs are in flight (or `docker kill` /
   `kubectl delete pod` one worker), confirm the reaper reclaims the abandoned job after its
   lease expires and a surviving worker completes it, with `attempts` incremented and no
   duplicate knodes (this is the noop-on-retry fix — assert the final knode count matches a
   clean run, not zero and not double).
2. **Dead-letter → retry.** Force a job to exhaust `max_attempts` (or wait for a real one to),
   confirm it lands in `dead` (never `failed` for an attempts-exhausted case — see the unified
   terminal taxonomy), confirm `di_jobs_dead_total` pages, then retry it via
   `POST /api/v1/jobs/{id}/retry` and confirm it completes.
3. **Horizontal scale-out.** Scale workers to 3+ replicas under load, confirm claims distribute
   across all of them (no single worker starves the others) and the per-tenant running cap holds
   even with multiple concurrent claimers.
4. **Backpressure.** Drive one tenant's active-job count to its cap (`max_active_jobs` tenant
   policy override, or the fleet default), confirm `429` with `Retry-After`, confirm other
   tenants are unaffected.
5. **Blob-backend misconfiguration.** Confirm a staging instance with `BLOB_BACKEND=none`
   refuses to boot in production posture, and that async ingest against a correctly-configured
   instance returns `503` cleanly if the blob store becomes unreachable mid-request (never a
   silent 202 for bytes that were not durably stored).
6. **RLS isolation under the real role split.** Confirm a plain `di_app`-authenticated session
   cannot read or claim another tenant's `di_job` rows, and that an unbound (no tenant GUC)
   session sees nothing — run this against staging's actual `di_app`/`di_worker_login`
   credentials, not just the test suite's.

## 4. Production flag flips

Set these on the production environment (values reflect the corrected design's defaults —
most are already the `Settings` default and only need overriding where a lower environment
intentionally relaxes them):

| Setting | Production value | Why |
|---|---|---|
| `DI_ENV` | `prod` (or `staging` for the staging tier) | Gates every check in §2 |
| `RLS_ENABLED` | `true` | Tenant isolation is not optional |
| `MIGRATIONS_MODE` | `verify` | Runtime instances hold no DDL rights; migrations run as a separate deploy step via `python -m di.migrate` under `PG_MIGRATION_USER` |
| `AUTH_ENABLED` | `true` | Every `/api/v1` route requires a key |
| `MASK_BY_DEFAULT` | `true` | Sensitive values masked unless a caller explicitly opts out |
| `ACCESS_AUDIT_ENABLED` | `true` | Read-side PII access audit trail |
| `ACCESS_AUDIT_STRICT` | `true` | A stalled audit writer refuses reads instead of silently dropping audit records |
| `INGEST_EMBEDDED_WORKER` | `false` | API replicas never run OCR/ingest in-process; dedicated `python -m di.worker` Deployment instead |
| `BLOB_BACKEND` | `postgres` or `s3` | Durable, multi-node-safe blob storage |
| `DI_BOOTSTRAP_API_KEY` | unset | Mint the first real key via `python -m di.tools.keys create`, then unset the bootstrap env var |
| `INSTANCE_FINGERPRINT_HMAC_KEY` | set, from Secret Manager | Salts multi-valued-fact instance fingerprints |

Deployment shape once these are set:
- **API** — a normal Deployment/Cloud Run service behind the load balancer.
- **Worker** — a separate Deployment (no Service/LB needed) running `python -m di.worker`;
  scale via replica count or a queue-depth-driven autoscaler (`count(*) FROM di_job WHERE
  status='queued'`); `SIGTERM` grace period ≥ `JOB_DRAIN_TIMEOUT_SECONDS` so a rolling deploy
  drains in-flight jobs instead of abandoning them to the reaper.
- **Migrations** — run once, out-of-band, via `python -m di.migrate` under `PG_MIGRATION_USER`
  before the new release's API/worker replicas roll out; `MIGRATIONS_MODE=verify` makes any
  replica that boots against an unmigrated (or drifted) schema refuse to start rather than run
  DDL itself.
