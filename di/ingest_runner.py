"""Background ingest runner — executes queued jobs off the request path.

``POST /ingest`` returns 202 immediately; this module owns actually running the pipeline and
recording every stage onto the job row, so progress survives the caller disconnecting.

Concurrency is bounded by ``settings.ingest_concurrency``: ingestion is CPU/OCR-heavy, and an
unbounded fan-out would starve read traffic on the same instance. Work is submitted to an asyncio
task set owned by the app lifespan, so shutdown can drain in-flight jobs.

This is deliberately an in-process runner, not a distributed queue. It removes the "dropped
connection loses the work" failure mode and gives every ingest a durable, pollable handle. A
multi-instance deployment should graduate to a real broker consuming ``di_job`` — the job model
and API contract here are already shaped for that.
"""
from __future__ import annotations

import asyncio
import logging

from di import jobs, observability
from di.config import get_settings
from di.jobs import JobEvent, JobStatus
from di.pipeline import ingest_document

logger = logging.getLogger(__name__)

#: Live tasks, kept referenced so the GC cannot collect a running job mid-flight.
_TASKS: set[asyncio.Task[None]] = set()
_SEMAPHORE: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(get_settings().ingest_concurrency)
    return _SEMAPHORE


async def submit_ingest_job(*, job_id: str, client_id: str, file_bytes: bytes, filename: str,
                            mime: str | None, external_document_id: str | None,
                            created_by: str | None) -> None:
    """Schedule a job. Returns as soon as the task is created (the caller already has the id)."""
    task = asyncio.create_task(
        _run(job_id=job_id, client_id=client_id, file_bytes=file_bytes, filename=filename,
             mime=mime, external_document_id=external_document_id, created_by=created_by),
        name=f"ingest:{job_id}",
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    observability.set_jobs_inflight(len(_TASKS))


async def _run(*, job_id: str, client_id: str, file_bytes: bytes, filename: str,
               mime: str | None, external_document_id: str | None,
               created_by: str | None) -> None:
    """Drive one job to a terminal state. Never raises: the job row carries the outcome."""
    async with _semaphore():
        try:
            await jobs.set_status(client_id, job_id, JobStatus.running, stage="ocr")
            doc_id: str | None = None
            version_id: str | None = None
            async for event in ingest_document(
                client_id, file_bytes, filename, mime=mime,
                external_document_id=external_document_id, created_by=created_by,
            ):
                await jobs.append_event(client_id, job_id, JobEvent(
                    stage=event.stage, status=event.status, detail=event.detail))
                if event.stage == "done":
                    doc_id = event.detail.get("doc_id") or doc_id
                    version_id = event.detail.get("version_id") or version_id
                await jobs.set_status(client_id, job_id, JobStatus.running, stage=event.stage)
            await jobs.set_status(client_id, job_id, JobStatus.succeeded, stage="done",
                                  doc_id=doc_id, version_id=version_id)
        except Exception as exc:  # noqa: BLE001 - the job row is the error channel
            logger.exception("ingest job %s failed", job_id)
            try:
                await jobs.set_status(client_id, job_id, JobStatus.failed, error=str(exc))
            except Exception:  # noqa: BLE001 - do not mask the original failure
                logger.exception("could not record failure for job %s", job_id)
        finally:
            observability.set_jobs_inflight(max(len(_TASKS) - 1, 0))


async def drain(timeout: float = 30.0) -> int:
    """Wait for in-flight jobs at shutdown. Returns the number still running after the timeout."""
    if not _TASKS:
        return 0
    logger.info("draining %d in-flight ingest job(s)", len(_TASKS))
    done, pending = await asyncio.wait(set(_TASKS), timeout=timeout)
    if pending:
        logger.warning("%d ingest job(s) still running at shutdown", len(pending))
    return len(pending)


__all__ = ["drain", "submit_ingest_job"]
