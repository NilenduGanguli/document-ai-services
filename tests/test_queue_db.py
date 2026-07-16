"""Live-DB integration tests for the durable job queue (migration 010).

Marked ``integration`` — requires the role-split-provisioned docker-compose database (di_owner /
di_worker_login must exist; ``docker compose up`` or ``tools/bootstrap_roles.sql`` first). Set
``DI_RUN_INTEGRATION=1``, ``PG_USER=di_owner``/``PG_PASSWORD`` (tenant-scoped setup via
``acquire()``, and RLS is deliberately NOT bypassed for di_owner — see the Phase-1 role split), and
``PG_WORKER_USER=di_worker_login``/``PG_WORKER_PASSWORD`` (cross-tenant queue mechanics via
``acquire_queue()`` — di_job's role-targeted ``worker_claim`` policy requires genuine ``di_worker``
membership; di_owner does not have it). ``RLS_ENABLED=true`` is required: with it off, ``acquire()``
never binds the tenant GUC either, and di_owner (not BYPASSRLS) would see zero rows for
everything, not just di_job.

Queue-role isolation itself (a plain di_app session cannot claim; an unbound connection sees
nothing) is proven in tests/test_rls_isolation.py, which already has the role-split-aware
connection/skip infrastructure — this file is about MECHANICS (claim/heartbeat/lease/reap/fencing),
not the isolation boundary.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from di.db import close_pool, close_worker_pool, init_pool, run_migrations
from di.jobs import JobStatus

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
async def _pool():
    try:
        await init_pool()
        await run_migrations()
    except Exception as e:  # noqa: BLE001 - any connect/auth/DDL failure -> skip, not fail
        pytest.skip(f"Postgres unavailable/unauthorized: {e}")

    # claim()/queue_stats() etc. are deliberately CROSS-TENANT (no client_id filter — a worker
    # processes any tenant's due work), so leftover rows from an earlier test function OR an
    # earlier `pytest` invocation against this same persistent database silently inflate any
    # assertion about total claimed/queued counts. Start every test with an empty di_job table.
    # Needs a worker-role connection: di_owner alone (not BYPASSRLS) sees zero rows for a
    # cross-tenant DELETE with no tenant GUC bound.
    from di.config import get_settings
    from di.db import init_worker_pool

    try:
        wpool = await init_worker_pool()
        async with wpool.acquire() as conn:
            await conn.execute(f'DELETE FROM "{get_settings().pg_schema}".di_job')
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"worker-role connection unavailable (set PG_WORKER_USER/PASSWORD to "
                    f"di_worker_login credentials): {e}")

    yield
    await close_worker_pool()
    await close_pool()


async def _bind(conn, client_id: str) -> None:
    """di_owner is deliberately NOT BYPASSRLS (Phase-1 role split): bind the tenant GUC before a
    raw INSERT that simulates a job a worker already claimed, exactly like
    di.db.acquire(client_id) would for the app's own connections."""
    await conn.execute("SELECT set_config('app.current_client_id', $1, false)", client_id)


def _cid() -> str:
    return f"test-q-{uuid.uuid4().hex[:8]}"


def _wid() -> str:
    return f"test-worker-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
async def test_enqueue_claim_complete_happy_path():
    from di import jobs

    cid = _cid()
    job = await jobs.enqueue(client_id=cid, kind="ingest", payload={"blob_uri": "x"})
    assert job.status is JobStatus.queued
    assert job.attempts == 0

    wid = _wid()
    claimed = await jobs.claim(wid, batch=4)
    ours = [c for c in claimed if c.client_id == cid]
    assert len(ours) == 1
    assert ours[0].id == job.id
    assert ours[0].attempts == 1
    assert ours[0].payload == {"blob_uri": "x"}

    ok = await jobs.complete(wid, job.id, JobStatus.succeeded, doc_id=str(uuid.uuid4()))
    assert ok is True
    fetched = await jobs.get_job(cid, job.id)
    assert fetched.status is JobStatus.succeeded
    assert fetched.finished_at is not None


async def test_enqueue_idempotency_key_returns_same_job():
    from di import jobs

    cid = _cid()
    a = await jobs.enqueue(client_id=cid, kind="ingest", idempotency_key="dup-1")
    b = await jobs.enqueue(client_id=cid, kind="ingest", idempotency_key="dup-1")
    assert a.id == b.id


async def test_public_job_never_carries_payload_over_the_wire():
    from di import jobs

    cid = _cid()
    job = await jobs.enqueue(client_id=cid, kind="ingest", payload={"blob_uri": "secret-path"})
    assert not hasattr(job, "payload")
    fetched = await jobs.get_job(cid, job.id)
    assert not hasattr(fetched, "payload")


# ---------------------------------------------------------------------------
# Concurrency: no double-claim, fencing
# ---------------------------------------------------------------------------
async def test_concurrent_claimers_never_double_claim():
    """20 DISTINCT tenants (one job each) — spreading across tenants keeps the per-tenant running
    cap (default 4) from bottlenecking this test, which is about claim-level row locking, not
    fairness/throughput (see test_per_tenant_running_cap_is_honored_on_a_single_claim_call for
    that)."""
    from di import jobs

    for _ in range(20):
        await jobs.enqueue(client_id=_cid(), kind="ingest")

    async def _claim_batch(wid: str) -> list[str]:
        claimed = await jobs.claim(wid, batch=5)
        return [c.id for c in claimed]

    worker_ids = [_wid() for _ in range(8)]
    results = await asyncio.gather(*(_claim_batch(w) for w in worker_ids))
    all_ids = [j for batch in results for j in batch]
    assert len(all_ids) == len(set(all_ids)), "the same job was claimed by more than one worker"
    assert len(all_ids) == 20


async def test_fenced_heartbeat_and_complete_after_reclaim_are_no_ops():
    from di import jobs

    cid = _cid()
    job = await jobs.enqueue(client_id=cid, kind="ingest")
    real_worker = _wid()
    [claimed] = await jobs.claim(real_worker, batch=1)

    # A different worker id (simulating a reclaim by the reaper + another worker's claim)
    # must never succeed against this job.
    impostor = _wid()
    renewed = await jobs.heartbeat(impostor, [job.id])
    assert renewed == []
    ok = await jobs.complete(impostor, job.id, JobStatus.succeeded)
    assert ok is False

    # The real owner's writes still apply.
    renewed = await jobs.heartbeat(real_worker, [job.id])
    assert renewed == [job.id]
    ok = await jobs.complete(real_worker, job.id, JobStatus.succeeded)
    assert ok is True


async def test_release_does_not_consume_an_attempt():
    from di import jobs

    cid = _cid()
    job = await jobs.enqueue(client_id=cid, kind="ingest")
    wid = _wid()
    [claimed] = await jobs.claim(wid, batch=1)
    assert claimed.attempts == 1

    released = await jobs.release(wid, [job.id])
    assert released == 1
    fetched = await jobs.get_job(cid, job.id)
    assert fetched.status is JobStatus.queued
    assert fetched.attempts == 0  # claim's +1 was undone by release's -1


async def test_retry_with_backoff_requeues_with_delay_and_keeps_the_attempt():
    from di import jobs

    cid = _cid()
    job = await jobs.enqueue(client_id=cid, kind="ingest")
    wid = _wid()
    [claimed] = await jobs.claim(wid, batch=1)
    assert claimed.attempts == 1

    ok = await jobs.retry_with_backoff(wid, job.id, error="transient", backoff_base=30.0,
                                       backoff_cap=3600.0)
    assert ok is True
    fetched = await jobs.get_job(cid, job.id)
    assert fetched.status is JobStatus.queued
    assert fetched.attempts == 1  # NOT decremented — this attempt genuinely happened
    assert fetched.error == "transient"

    # Not immediately reclaimable: run_after is in the future.
    reclaimed = await jobs.claim(wid, batch=10, kinds=("ingest",))
    assert job.id not in [c.id for c in reclaimed]


# ---------------------------------------------------------------------------
# Reaper: lease expiry, dead-lettering, unified taxonomy
# ---------------------------------------------------------------------------
async def test_reap_requeues_expired_lease_and_increments_attempts():
    from di import store
    from di.config import get_settings

    s = get_settings().pg_schema
    cid = _cid()
    job_id = str(uuid.uuid4())
    pool = await init_pool()
    async with pool.acquire() as conn:
        await _bind(conn, cid)
        await conn.execute(
            f'INSERT INTO "{s}".di_job (id, client_id, status, kind, attempts, max_attempts, '
            " locked_by, lease_expires_at) "
            "VALUES ($1, $2, 'running', 'ingest', 1, 3, 'stale-worker', now() - interval '1 hour')",
            uuid.UUID(job_id), cid,
        )
    from di import jobs

    requeued_kinds, dead_kinds = await jobs.reap(backoff_base=1.0, backoff_cap=60.0)
    assert "ingest" in requeued_kinds
    fetched = await jobs.get_job(cid, job_id)
    assert fetched.status is JobStatus.queued
    assert fetched.attempts == 1  # reap does not bump attempts (claim already did, at claim time)
    del store  # unused import guard for readability; keep symmetry with other DB-touching tests


async def test_reap_dead_letters_when_attempts_exhausted():
    from di.config import get_settings

    s = get_settings().pg_schema
    cid = _cid()
    job_id = str(uuid.uuid4())
    pool = await init_pool()
    async with pool.acquire() as conn:
        await _bind(conn, cid)
        await conn.execute(
            f'INSERT INTO "{s}".di_job (id, client_id, status, kind, attempts, max_attempts, '
            " locked_by, lease_expires_at) "
            "VALUES ($1, $2, 'running', 'ingest', 3, 3, 'stale-worker', now() - interval '1 hour')",
            uuid.UUID(job_id), cid,
        )
    from di import jobs

    requeued_kinds, dead_kinds = await jobs.reap(backoff_base=1.0, backoff_cap=60.0)
    assert "ingest" in dead_kinds
    fetched = await jobs.get_job(cid, job_id)
    assert fetched.status is JobStatus.dead
    assert fetched.finished_at is not None


async def test_reap_rescues_pre_010_null_lease_rows():
    """Rows orphaned by an old-world crash (no lease at all) are rescued after 15 minutes of
    silence, not stuck 'running' forever."""
    from di.config import get_settings

    s = get_settings().pg_schema
    cid = _cid()
    job_id = str(uuid.uuid4())
    pool = await init_pool()
    async with pool.acquire() as conn:
        await _bind(conn, cid)
        await conn.execute(
            f'INSERT INTO "{s}".di_job (id, client_id, status, kind, attempts, max_attempts, '
            " updated_at) "
            "VALUES ($1, $2, 'running', 'ingest', 0, 3, now() - interval '20 minutes')",
            uuid.UUID(job_id), cid,
        )
    from di import jobs

    requeued_kinds, _dead_kinds = await jobs.reap(backoff_base=1.0, backoff_cap=60.0)
    assert "ingest" in requeued_kinds
    fetched = await jobs.get_job(cid, job_id)
    assert fetched.status is JobStatus.queued


# ---------------------------------------------------------------------------
# Fairness: soft per-tenant running cap
# ---------------------------------------------------------------------------
async def test_per_tenant_running_cap_is_honored_on_a_single_claim_call():
    from di import jobs
    from di.config import get_settings

    cid = _cid()
    for _ in range(10):
        await jobs.enqueue(client_id=cid, kind="ingest")
    settings = get_settings()
    cap = settings.ingest_tenant_max_running
    wid = _wid()
    claimed = await jobs.claim(wid, batch=20)
    ours = [c for c in claimed if c.client_id == cid]
    assert len(ours) <= cap


# ---------------------------------------------------------------------------
# Admin: retry / cancel
# ---------------------------------------------------------------------------
async def test_cancel_only_affects_queued_jobs():
    from di import jobs

    cid = _cid()
    job = await jobs.enqueue(client_id=cid, kind="ingest")
    canceled = await jobs.cancel(cid, job.id)
    assert canceled is not None
    assert canceled.status is JobStatus.canceled

    # Cancel again -> None (no longer 'queued')
    again = await jobs.cancel(cid, job.id)
    assert again is None


async def test_cancel_does_not_affect_running_jobs():
    from di import jobs

    cid = _cid()
    job = await jobs.enqueue(client_id=cid, kind="ingest")
    wid = _wid()
    await jobs.claim(wid, batch=1)
    result = await jobs.cancel(cid, job.id)
    assert result is None  # job is 'running', not 'queued' — no preemption


async def test_retry_requeues_a_dead_job_and_resets_attempts():
    from di.config import get_settings

    s = get_settings().pg_schema
    cid = _cid()
    job_id = str(uuid.uuid4())
    pool = await init_pool()
    async with pool.acquire() as conn:
        await _bind(conn, cid)
        await conn.execute(
            f'INSERT INTO "{s}".di_job (id, client_id, status, kind, attempts, max_attempts, '
            " error, finished_at) "
            "VALUES ($1, $2, 'dead', 'ingest', 3, 3, 'poison document', now())",
            uuid.UUID(job_id), cid,
        )
    from di import jobs

    retried = await jobs.retry(cid, job_id)
    assert retried is not None
    assert retried.status is JobStatus.queued
    assert retried.attempts == 0
    assert retried.error is None
    assert retried.finished_at is None


async def test_retry_only_affects_dead_jobs():
    from di import jobs

    cid = _cid()
    job = await jobs.enqueue(client_id=cid, kind="ingest")  # status='queued', not 'dead'
    result = await jobs.retry(cid, job.id)
    assert result is None


# ---------------------------------------------------------------------------
# queue_stats
# ---------------------------------------------------------------------------
async def test_queue_stats_reflects_enqueued_jobs():
    from di import jobs

    cid = _cid()
    for _ in range(3):
        await jobs.enqueue(client_id=cid, kind="ingest")
    stats = await jobs.queue_stats()
    ingest_queued = next(
        (s for s in stats if s["kind"] == "ingest" and s["status"] == "queued"), None)
    assert ingest_queued is not None
    assert ingest_queued["n"] >= 3


# ---------------------------------------------------------------------------
# The most dangerous correctness case: noop-on-retry closed by ingest_complete
# ---------------------------------------------------------------------------
async def test_kill_between_create_version_and_insert_knodes_rebuilds_not_noops():
    """Simulates a worker crash in the exact window the corrected design's delta 1 closes: a
    version row commits (ingest_complete=false) but knodes never get written before the crash. A
    retry with the SAME content_hash must resume — not report a noop, and not leave the version
    permanently incomplete."""
    from di import store

    cid = _cid()
    doc_id = str(uuid.uuid4())
    s = store._schema()
    pool = await init_pool()
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.current_client_id', $1, false)", cid)
        await conn.execute(
            f'INSERT INTO "{s}".di_documents (id, client_id, document_name) '
            "VALUES ($1, $2, 'acta.pdf')", uuid.UUID(doc_id), cid,
        )

    # First "attempt": create_version commits, then the simulated crash — no knodes ever written.
    v1 = await store.create_version(cid, doc_id, content_hash="deadbeef")
    assert v1.is_noop is False
    assert v1.resume is False

    # Retry with the SAME content_hash (at-least-once redelivery of the same payload).
    v2 = await store.create_version(cid, doc_id, content_hash="deadbeef")
    assert v2.is_noop is False, "a hash match against an INCOMPLETE version must never noop"
    assert v2.resume is True
    assert v2.version_id == v1.version_id
    assert v2.version_no == v1.version_no

    # The pipeline would now call delete_version_artifacts (harmless no-op here — nothing was
    # written) and rebuild, then mark_version_complete.
    await store.delete_version_artifacts(cid, v2.version_id)
    await store.mark_version_complete(cid, v2.version_id)

    # A THIRD "retry" after completion must now be a true noop.
    v3 = await store.create_version(cid, doc_id, content_hash="deadbeef")
    assert v3.is_noop is True
    assert v3.version_id == v1.version_id
