"""Dedicated ingest worker — claims ``di_job`` rows and drives the pipeline to a terminal state.

Runs either as a standalone process (``python -m di.worker``) or embedded in the API lifespan
(single-node/compose demo, ``ingest_embedded_worker=true``). One implementation, two deployment
shapes — there is no second execution path to drift, unlike the deleted ``di/ingest_runner.py``.

``Consumer`` owns four concurrent loops: claim (LISTEN ``di_job_new`` with a poll-interval
fallback), heartbeat (renew leases at ``lease/4``), reaper (requeue expired leases with backoff,
dead-letter exhausted attempts), and dispatch (drive each claimed job's pipeline). Every
worker-side DB write is fenced on ``locked_by`` — a job reclaimed by the reaper while this process
still thinks it owns it writes zero rows on its next heartbeat/event/status write and the local
task is canceled immediately, so a zombie worker can never resurrect a job or interleave its
events with the attempt that actually owns it now.
"""
from __future__ import annotations

import asyncio
import logging
import random
import signal
import time
from contextlib import suppress

import asyncpg

from di import jobs, observability, store
from di.config import Settings, get_settings
from di.db import (
    _ssl_context,
    close_pool,
    close_worker_pool,
    init_pool,
    init_worker_pool,
    pgvector_available,
)
from di.jobs import ClaimedJob, JobEvent, JobStatus
from di.models import KNode
from di.pipeline import _embed_areps, ingest_document
from di.retrieval_client import get_retrieval_client
from di.storage import BlobNotFound, BlobRef, get_blob_store
from di.subtree import arep as arep_mod

logger = logging.getLogger(__name__)

#: Exceptions that must never be retried — jump straight to 'failed' regardless of attempts left.
_NON_RETRYABLE: tuple[type[Exception], ...] = (BlobNotFound,)

#: Message stamped on pre-010 orphaned rows: their bytes died with the old in-process runner
#: (di/ingest_runner.py's in-memory handoff), so they are claimed-and-failed explicitly rather
#: than mistaken for a genuine payload bug — excluded from poison-pill alerting by this message.
_PAYLOAD_LOST_ERROR = "payload lost pre-durable-queue upgrade"


class Consumer:
    """Claims and executes queued jobs, honoring lease/heartbeat/reap semantics."""

    def __init__(self, *, worker_id: str | None = None) -> None:
        self.settings = get_settings()
        self.worker_id = worker_id or jobs.worker_id()
        self._tasks: dict[str, asyncio.Task] = {}
        self._background: list[asyncio.Task] = []
        self._stopping = False
        self._wake = asyncio.Event()
        self._listen_conn: asyncpg.Connection | None = None

    async def start(self) -> None:
        """Start the claim/heartbeat/reaper/listen loops as background tasks."""
        await init_worker_pool(self.settings)
        self._background = [
            asyncio.create_task(self._claim_loop(), name="worker:claim"),
            asyncio.create_task(self._heartbeat_loop(), name="worker:heartbeat"),
            asyncio.create_task(self._reaper_loop(), name="worker:reaper"),
            asyncio.create_task(self._listen_loop(), name="worker:listen"),
        ]
        logger.info("worker %s started (concurrency=%d)", self.worker_id,
                   self.settings.ingest_concurrency)

    async def drain(self, timeout: float | None = None) -> int:
        """Stop claiming, wait for in-flight jobs, then voluntarily release whatever remains.

        Returns:
            The number of jobs still in flight when the timeout expired (released, not lost —
            they go back to 'queued' for another worker to pick up, at no attempt cost).
        """
        self._stopping = True
        for t in self._background:
            t.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

        timeout = timeout if timeout is not None else self.settings.job_drain_timeout_seconds
        remaining = 0
        if self._tasks:
            logger.info("draining %d in-flight job(s)", len(self._tasks))
            done, pending = await asyncio.wait(list(self._tasks.values()), timeout=timeout)
            if pending:
                logger.warning("%d job(s) still running at shutdown; canceling + releasing",
                               len(pending))
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            remaining_ids = list(self._tasks.keys())
            if remaining_ids:
                remaining = await jobs.release(self.worker_id, remaining_ids)

        if self._listen_conn is not None:
            with suppress(Exception):
                await self._listen_conn.close()
            self._listen_conn = None
        return remaining

    # ------------------------------------------------------------------
    # Claim loop
    # ------------------------------------------------------------------
    async def _claim_loop(self) -> None:
        while not self._stopping:
            try:
                free = self.settings.job_claim_batch - len(self._tasks)
                # Claim at most the free local capacity: a claimed-but-locally-queued job would
                # otherwise burn its lease unheartbeated, or (if heartbeated anyway) sit hostage
                # to a stuck worker instead of being available for another one to claim.
                if free > 0:
                    claimed = await jobs.claim(self.worker_id, batch=free)
                    for job in claimed:
                        self._dispatch(job)
                    if claimed:
                        continue  # try again immediately in case more is queued
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the claim loop must survive a transient DB hiccup
                logger.exception("claim loop error")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=self.settings.job_poll_interval_seconds)
            self._wake.clear()

    def _dispatch(self, job: ClaimedJob) -> None:
        observability.observe_job_claimed(job.kind)
        task = asyncio.create_task(self._run_job(job), name=f"job:{job.id}")
        self._tasks[job.id] = task
        def _cleanup(_t: asyncio.Task, jid: str = job.id) -> None:
            self._tasks.pop(jid, None)

        task.add_done_callback(_cleanup)
        observability.set_jobs_inflight(len(self._tasks))

    # ------------------------------------------------------------------
    # Heartbeat loop — renews leases; cancels locally any task that was reclaimed
    # ------------------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        interval = max(1.0, self.settings.job_lease_seconds / 4)
        while not self._stopping:
            await asyncio.sleep(interval)
            if not self._tasks:
                continue
            try:
                renewed = set(await jobs.heartbeat(self.worker_id, list(self._tasks.keys())))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed heartbeat round must not crash the worker
                logger.exception("heartbeat round failed")
                continue
            for job_id, task in list(self._tasks.items()):
                if job_id not in renewed and not task.done():
                    logger.warning("job %s lease lost (reclaimed elsewhere); canceling locally",
                                   job_id)
                    observability.observe_lease_lost()
                    task.cancel()

    # ------------------------------------------------------------------
    # Reaper loop — every worker runs this; idempotent (WHERE status='running' + row-level races
    # simply mean at most one worker's UPDATE matches each row).
    # ------------------------------------------------------------------
    async def _reaper_loop(self) -> None:
        base_interval = self.settings.job_reaper_interval_seconds
        while not self._stopping:
            jitter = base_interval * random.uniform(0.8, 1.2)
            await asyncio.sleep(jitter)
            try:
                requeued_kinds, dead_kinds = await jobs.reap(
                    backoff_base=self.settings.job_retry_base_seconds,
                    backoff_cap=self.settings.job_retry_max_seconds)
                for kind in set(requeued_kinds):
                    observability.observe_job_retried(kind, requeued_kinds.count(kind))
                for kind in set(dead_kinds):
                    observability.observe_job_dead(kind, dead_kinds.count(kind))
                if requeued_kinds or dead_kinds:
                    logger.info("reap: requeued=%d dead=%d", len(requeued_kinds), len(dead_kinds))
                # Refresh the queue depth/age gauges here too (not just this worker's own claim
                # activity), so the metric stays live on this cadence even during a quiet period —
                # and every worker doing this means the metric survives any single worker's death.
                observability.set_queue_stats(await jobs.queue_stats())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad reap cycle must not stop future ones
                logger.exception("reaper cycle failed")

    # ------------------------------------------------------------------
    # LISTEN loop — a dedicated, non-pooled connection: the pool recycles idle connections after
    # 300s (di.db.init_pool's max_inactive_connection_lifetime), which would silently kill LISTEN.
    # ------------------------------------------------------------------
    async def _listen_loop(self) -> None:
        s = self.settings
        while not self._stopping:
            try:
                conn = await asyncpg.connect(
                    host=s.pg_host, port=s.pg_port,
                    user=s.pg_worker_user or s.pg_user,
                    password=s.pg_worker_password or s.pg_password or None,
                    database=s.pg_database, ssl=_ssl_context(s),
                )
                self._listen_conn = conn
                await conn.add_listener("di_job_new", lambda *_a: self._wake.set())
                while not self._stopping:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - correctness never depends on LISTEN; poll is the floor
                logger.warning("LISTEN connection lost; relying on poll_interval until it recovers",
                               exc_info=True)
                await asyncio.sleep(5)
            finally:
                if self._listen_conn is not None:
                    with suppress(Exception):
                        await self._listen_conn.close()
                    self._listen_conn = None

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------
    async def _run_job(self, job: ClaimedJob) -> None:
        started = time.perf_counter()
        try:
            if job.kind == "ingest":
                await self._run_ingest(job)
            elif job.kind == "arep":
                await self._run_arep(job)
            else:
                await jobs.complete(self.worker_id, job.id, JobStatus.dead,
                                    error=f"unknown job kind: {job.kind!r}")
            observability.observe_job_duration(job.kind, time.perf_counter() - started)
        except asyncio.CancelledError:
            # Either reclaimed (heartbeat mismatch) or shutdown drain — the reaper or release()
            # already owns this job's fate; do not mark it complete from here.
            raise
        except Exception as exc:  # noqa: BLE001 - the job row is the error channel
            logger.exception("job %s (%s) failed", job.id, job.kind)
            await self._finish_failed(job, exc)
            observability.observe_job_duration(job.kind, time.perf_counter() - started)
        finally:
            observability.set_jobs_inflight(max(len(self._tasks) - 1, 0))

    async def _finish_failed(self, job: ClaimedJob, exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, _NON_RETRYABLE):
            await jobs.complete(self.worker_id, job.id, JobStatus.failed, error=error)
            return
        if job.attempts >= job.max_attempts:
            # Unified terminal taxonomy: attempts-exhausted always ends 'dead' (alertable via
            # di_jobs_dead_total), whether it got there via a clean in-process exception (here) or
            # via lease-expiry reclaim (di.jobs.reap) — 'failed' is reserved for the non-retryable
            # branch above, so the dead>0 alert never misses a deterministic poison document.
            await jobs.complete(self.worker_id, job.id, JobStatus.dead, error=error)
            observability.observe_job_dead(job.kind)
            return
        await jobs.retry_with_backoff(
            self.worker_id, job.id, error=error,
            backoff_base=self.settings.job_retry_base_seconds,
            backoff_cap=self.settings.job_retry_max_seconds)
        observability.observe_job_retried(job.kind)

    async def _run_ingest(self, job: ClaimedJob) -> None:
        client_id = job.client_id
        payload = job.payload
        blob_uri = payload.get("blob_uri")
        if not payload or not blob_uri:
            await jobs.complete(self.worker_id, job.id, JobStatus.dead, error=_PAYLOAD_LOST_ERROR)
            return

        try:
            file_bytes = await get_blob_store().get(blob_uri, client_id=client_id)
        except BlobNotFound as exc:
            await jobs.complete(self.worker_id, job.id, JobStatus.failed,
                                error=f"blob not found: {exc}")
            return

        applied = await jobs.set_status(client_id, job.id, JobStatus.running, stage="ocr",
                                        locked_by=self.worker_id)
        if not applied:
            return  # reclaimed before we even started

        doc_id, version_id = job.doc_id, job.version_id
        blob_ref = BlobRef(uri=blob_uri, backend=payload.get("blob_backend") or "postgres",
                           size=payload.get("size") or len(file_bytes))
        async for event in ingest_document(
            client_id, file_bytes,
            payload.get("filename") or job.document_name or "upload",
            mime=payload.get("mime"), external_document_id=payload.get("external_document_id"),
            created_by=payload.get("created_by"), blob=blob_ref,
            content_hash=payload.get("content_hash"),
        ):
            applied = await jobs.append_event(
                client_id, job.id,
                JobEvent(stage=event.stage, status=event.status, detail=event.detail,
                        attempt=job.attempts),
                locked_by=self.worker_id)
            if not applied:
                raise asyncio.CancelledError(f"job {job.id} reclaimed mid-flight")
            if event.stage == "done":
                doc_id = event.detail.get("doc_id") or doc_id
                version_id = event.detail.get("version_id") or version_id
            await jobs.set_status(client_id, job.id, JobStatus.running, stage=event.stage,
                                  locked_by=self.worker_id)

        ok = await jobs.complete(self.worker_id, job.id, JobStatus.succeeded,
                                 doc_id=doc_id, version_id=version_id)
        if ok and not self.settings.blob_retain_after_ingest:
            with suppress(Exception):
                await get_blob_store().delete(blob_uri, client_id=client_id)

    async def _run_arep(self, job: ClaimedJob) -> None:
        client_id = job.client_id
        payload = job.payload
        doc_id, version_id = payload.get("doc_id"), payload.get("version_id")
        raw_nodes = payload.get("nodes") or []
        if not doc_id or not version_id or not raw_nodes:
            await jobs.complete(self.worker_id, job.id, JobStatus.dead,
                                error="arep payload missing doc_id/version_id/nodes")
            return
        applied = await jobs.set_status(client_id, job.id, JobStatus.running, stage="arep",
                                        locked_by=self.worker_id)
        if not applied:
            return

        # Idempotent-by-replace: a retried arep job (lease expiry mid-generation) must not
        # duplicate reps on top of a partial prior attempt.
        await store.delete_version_areps(client_id, version_id)

        nodes = [KNode.model_validate(n) for n in raw_nodes]
        client = get_retrieval_client(self.settings)
        try:
            reps = await arep_mod.generate_areps(
                nodes, client=client, languages=self.settings.supported_languages)
            if await pgvector_available():
                await _embed_areps(reps, client)
            await store.insert_areps(reps)
        finally:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()

        await jobs.complete(self.worker_id, job.id, JobStatus.succeeded,
                            doc_id=doc_id, version_id=version_id)


async def _run_migrations_if_configured(settings: Settings) -> None:
    """Mirror di.app's startup migration handling: whichever process (API or worker) boots first
    under the advisory lock does the work; the other blocks then finds the ledger already
    current. A dedicated worker deployed standalone (no API process at all) still needs this —
    otherwise it would try to claim against a schema that may not have the queue columns yet.
    """
    from di.db import open_migration_connection, run_migrations, verify_migrations

    if settings.migrations_mode == "auto":
        conn = await open_migration_connection(settings)
        try:
            await run_migrations(settings, connection=conn)
        finally:
            await conn.close()
    elif settings.migrations_mode == "verify":
        await verify_migrations(settings)
    # "off": nothing to do — matches di.app's _startup().


async def _amain() -> None:
    logging.basicConfig(level=get_settings().di_log_level)
    settings = get_settings()
    await init_pool(settings)
    try:
        await _run_migrations_if_configured(settings)
    except Exception:
        logger.exception("worker could not confirm migrations; exiting")
        await close_pool()
        raise
    consumer = Consumer()
    await consumer.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):  # Windows lacks add_signal_handler for these
            loop.add_signal_handler(sig, stop.set)

    _start_metrics_server(settings.worker_metrics_port)
    logger.info("worker %s ready", consumer.worker_id)
    await stop.wait()
    logger.info("worker %s draining", consumer.worker_id)
    await consumer.drain()
    await close_worker_pool()
    await close_pool()


def _start_metrics_server(port: int) -> None:
    """Best-effort: a metrics/health endpoint for the container healthcheck. Never fatal — a
    worker must still run without prometheus_client installed."""
    try:
        import prometheus_client

        prometheus_client.start_http_server(port)
        logger.info("worker metrics/health on :%d", port)
    except Exception:  # noqa: BLE001
        logger.warning("could not start worker metrics server on :%d", port, exc_info=True)


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
