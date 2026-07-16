"""Durable, DB-backed job queue for asynchronous ingestion.

``di_job`` (migration 005) plus the queue columns from migration 010 (kind, payload, priority,
attempts, run_after, lease_expires_at, locked_by) makes Postgres the queue: workers claim due rows
with ``FOR UPDATE SKIP LOCKED``, heartbeat a lease while working, and a reaper requeues expired
leases with backoff until ``max_attempts``, then dead-letters them.

Design notes:
- claim/heartbeat/complete/release/reap/queue_stats run under ``di.db.acquire_queue()`` — the
  worker-role pool — since claiming needs cross-tenant visibility of ``di_job`` before any tenant
  GUC exists (di_job's role-targeted ``worker_claim`` policy grants this to ``di_worker`` members,
  not a GUC any session could set for itself);
- every worker-side write (heartbeat, complete, and the pipeline's own append_event/set_status) is
  FENCED on ``locked_by`` + ``status='running'``: a zombie worker whose job was already reclaimed
  by the reaper writes zero rows and must stop, rather than silently flipping a requeued job back
  to 'running' with no lease;
- the public ``Job`` model / ``_JOB_COLS`` deliberately exclude ``payload`` — it carries internal
  blob URIs and file paths that must never reach an API caller;
- ``append_event`` concatenates server-side (``events = events || $x::jsonb``) rather than doing a
  read-modify-write, so concurrent pipeline stages cannot clobber one another's events;
- ``list_jobs`` uses keyset (not OFFSET) pagination over ``(created_at DESC, id DESC)``, so pages
  stay stable and cheap while new jobs are being inserted at the head of the feed.

Claim fairness: plain global ``priority, run_after, created_at, id`` FIFO ORDERING (not the
window-function cross-tenant round-robin INTERLEAVING the original design specified — that query
is materially more complex and untested at any real scale, so it is deferred until measured need,
per docs/specs/2026-07-15-enterprise-scale-plan.md's corrected delta 10). The per-tenant RUNNING
CAP is a separate concern from ordering and IS enforced within one claim() call (a
``row_number() OVER (PARTITION BY client_id ...)`` ranks each tenant's candidates against how many
of its jobs are already running) — "soft" only in the sense that two concurrent claim() calls each
snapshot before either commits, so back-to-back concurrent claimers can transiently push one
tenant slightly over the cap. FIFO ordering plus the (within-call-enforced, cross-call-soft) cap
is the v1 fairness story.
"""
from __future__ import annotations

import base64
import random
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import asyncpg
from pydantic import BaseModel, Field

from di.config import get_settings
from di.db import acquire, acquire_queue

__all__ = [
    "ClaimedJob",
    "Job",
    "JobEvent",
    "JobStatus",
    "append_event",
    "cancel",
    "claim",
    "complete",
    "count_active_and_today",
    "enqueue",
    "find_by_idempotency",
    "get_job",
    "heartbeat",
    "list_jobs",
    "purge_client_jobs",
    "queue_stats",
    "reap",
    "release",
    "retry",
    "retry_with_backoff",
    "set_status",
    "worker_id",
]

#: Page-size guard rails for :func:`list_jobs`.
_MIN_LIMIT = 1
_MAX_LIMIT = 200

#: Public column list — explicit (not ``*``) so row -> model mapping is stable, and so
#: ``payload`` (internal blob URIs / file paths) never reaches an API caller.
_JOB_COLS = (
    "id, client_id, status, stage, kind, document_name, doc_id, version_id, error, "
    "idempotency_key, events, attempts, max_attempts, created_at, updated_at, finished_at"
)

#: Internal column list for claim()/reap() results — includes payload + queue-internal fields the
#: worker needs but the public Job model must never expose.
_CLAIM_COLS = (
    "id, client_id, status, stage, kind, payload, document_name, doc_id, version_id, "
    "idempotency_key, attempts, max_attempts, priority, run_after, locked_by, created_at"
)
#: Same columns, qualified to the updated table's alias — claim()'s UPDATE ... FROM (subquery) c
#: RETURNING clause evaluates against both the target row and the FROM-list, so an unqualified
#: `id` (present in both `di_job` and the subquery result `c`) is ambiguous.
_CLAIM_COLS_QUALIFIED = ", ".join(f"j.{col}" for col in
                                  (c.strip() for c in _CLAIM_COLS.split(",")))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    dead = "dead"          # attempts exhausted (either exception-driven or lease-expiry-driven)
    canceled = "canceled"  # operator-requested, queued -> canceled only


#: Statuses after which no further work happens; these stamp ``finished_at``.
_TERMINAL = frozenset({JobStatus.succeeded, JobStatus.failed, JobStatus.dead, JobStatus.canceled})

#: Non-terminal statuses, derived from the enum rather than hardcoded — when a later phase adds
#: another state, "active" tracks it automatically instead of silently under-counting against a
#: stale literal list.
_ACTIVE_STATUSES = tuple(s.value for s in JobStatus if s not in _TERMINAL)


class JobEvent(BaseModel):
    """One durable pipeline stage event (the persisted analogue of ``IngestEvent``)."""

    stage: str
    status: str = "ok"
    detail: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    #: Which claim attempt produced this event — at-least-once retry means a job's timeline can
    #: span more than one attempt; without this the events read as one contiguous (and
    #: misleading) run.
    attempt: int = 1


class Job(BaseModel):
    """An ingestion job and its accumulated stage events. Never carries ``payload``."""

    id: str
    client_id: str
    status: JobStatus
    stage: str | None = None
    kind: str = "ingest"
    document_name: str | None = None
    doc_id: str | None = None
    version_id: str | None = None
    error: str | None = None
    idempotency_key: str | None = None
    events: list[JobEvent] = Field(default_factory=list)
    attempts: int = 0
    max_attempts: int = 3
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class ClaimedJob(BaseModel):
    """A job as claimed by a worker — includes ``payload`` and queue-internal fields. Never
    serialized to the API; ``di.worker`` is the only consumer."""

    id: str
    client_id: str
    status: JobStatus
    stage: str | None = None
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    document_name: str | None = None
    doc_id: str | None = None
    version_id: str | None = None
    idempotency_key: str | None = None
    attempts: int
    max_attempts: int
    priority: int
    run_after: datetime
    locked_by: str | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _schema() -> str:
    return get_settings().pg_schema


def _as_uuid(value: str | None) -> uuid.UUID | None:
    """Coerce an optional id string to a UUID.

    Raises:
        ValueError: If ``value`` is a non-empty string that is not a valid UUID.
    """
    return uuid.UUID(value) if value else None


def _clamp_limit(limit: int) -> int:
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


def _encode_cursor(created_at: datetime, job_id: str) -> str:
    raw = f"{created_at.isoformat()}|{job_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode an opaque cursor produced by :func:`_encode_cursor`.

    Cursors arrive from clients and are therefore untrusted: every malformed shape raises
    ``ValueError`` so callers can map it to a 400 rather than leaking a driver error.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:  # binascii.Error subclasses ValueError
        raise ValueError(f"malformed cursor: {cursor!r}") from exc

    ts_text, sep, id_text = raw.partition("|")
    if not sep:
        raise ValueError(f"malformed cursor (missing separator): {cursor!r}")
    try:
        created_at = datetime.fromisoformat(ts_text)
        job_id = str(uuid.UUID(id_text))
    except ValueError as exc:
        raise ValueError(f"malformed cursor (bad position): {cursor!r}") from exc
    return created_at, job_id


def _rowcount(command_tag: str) -> int:
    """Parse an asyncpg command tag (e.g. ``"DELETE 3"``) into its affected-row count."""
    parts = command_tag.split()
    return int(parts[-1]) if parts and parts[-1].isdigit() else 0


def _row_to_job(row: asyncpg.Record) -> Job:
    return Job(
        id=str(row["id"]),
        client_id=row["client_id"],
        status=JobStatus(row["status"]),
        stage=row["stage"],
        kind=row["kind"],
        document_name=row["document_name"],
        doc_id=str(row["doc_id"]) if row["doc_id"] else None,
        version_id=str(row["version_id"]) if row["version_id"] else None,
        error=row["error"],
        idempotency_key=row["idempotency_key"],
        events=[JobEvent.model_validate(e) for e in (row["events"] or [])],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )


def _row_to_claimed(row: asyncpg.Record) -> ClaimedJob:
    return ClaimedJob(
        id=str(row["id"]),
        client_id=row["client_id"],
        status=JobStatus(row["status"]),
        stage=row["stage"],
        kind=row["kind"],
        payload=dict(row["payload"] or {}),
        document_name=row["document_name"],
        doc_id=str(row["doc_id"]) if row["doc_id"] else None,
        version_id=str(row["version_id"]) if row["version_id"] else None,
        idempotency_key=row["idempotency_key"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        priority=row["priority"],
        run_after=row["run_after"],
        locked_by=row["locked_by"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Admission — enqueue
# ---------------------------------------------------------------------------
async def enqueue(*, client_id: str, kind: str = "ingest", payload: dict[str, Any] | None = None,
                  document_name: str | None = None, idempotency_key: str | None = None,
                  priority: int = 100, max_attempts: int | None = None) -> Job:
    """Create a ``queued`` job carrying ``payload`` (e.g. the blob-at-accept pointer).

    When ``idempotency_key`` is supplied and a job already exists for it, the existing job is
    returned instead of raising — the unique index on ``(client_id, idempotency_key)`` makes this
    safe under concurrent retries of the same request.
    """
    s = _schema()
    settings = get_settings()
    max_attempts = max_attempts if max_attempts is not None else settings.job_max_attempts
    try:
        async with acquire(client_id) as conn:
            row = await conn.fetchrow(
                f'INSERT INTO "{s}".di_job '
                "(id, client_id, status, kind, payload, document_name, idempotency_key, "
                " priority, max_attempts) "
                f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING {_JOB_COLS}",
                str(uuid.uuid4()), client_id, JobStatus.queued.value, kind, payload or {},
                document_name, idempotency_key, priority, max_attempts,
            )
            if row is not None:
                # Wake-latency optimization only — correctness never depends on this arriving;
                # job_poll_interval_seconds is the floor. Same connection/transaction as the
                # insert, so it fires exactly once the row is actually committed.
                await conn.execute("SELECT pg_notify('di_job_new', $1)", str(row["id"]))
    except asyncpg.UniqueViolationError:
        if idempotency_key is None:
            raise
        existing = await find_by_idempotency(client_id, idempotency_key)
        if existing is None:  # pragma: no cover - conflicting row vanished mid-retry
            raise
        return existing
    if row is None:  # pragma: no cover - INSERT ... RETURNING always yields a row
        raise RuntimeError("di_job insert returned no row")
    return _row_to_job(row)


# ---------------------------------------------------------------------------
# Queue mechanics — worker-role pool, no tenant GUC (worker_claim policy)
# ---------------------------------------------------------------------------
async def claim(worker_id: str, *, batch: int, kinds: tuple[str, ...] = ("ingest", "arep"),
                ) -> list[ClaimedJob]:
    """Claim up to ``batch`` due jobs across every tenant.

    Global FIFO by ``(priority, run_after, created_at, id)`` — not the window-function
    cross-tenant round-robin INTERLEAVING the original design specified for claim ORDER (see the
    module docstring for why: deferred until measured, per the corrected design's delta 10).

    The per-tenant RUNNING CAP is a separate concern from ordering and IS enforced within this
    call, not merely across separate calls: a ``row_number() OVER (PARTITION BY client_id ...)``
    ranks each tenant's queued candidates, joined against how many of that tenant's jobs are
    already 'running', so a single claim() cannot hand one tenant more than
    ``ingest_tenant_max_running`` slots even when its backlog alone exceeds ``batch``. It is still
    a SOFT cap in the sense the corrected design means: two concurrent claim() calls each compute
    their eligible set from a snapshot BEFORE either commits, so back-to-back concurrent claimers
    can transiently push one tenant slightly over the cap — bounded slop, not a hard guarantee.

    Window functions cannot combine with ``FOR UPDATE`` in one query level, so this is two-level:
    an outer, lock-free candidate selection (cap-aware, FIFO-ordered) feeds an inner
    ``FOR UPDATE SKIP LOCKED`` + a ``status = 'queued'`` re-check, so concurrent claimers never
    double-claim a row — a lost race there simply shrinks this call's batch.
    """
    if batch <= 0:
        return []
    settings = get_settings()
    s = _schema()
    # A wider-than-batch candidate window absorbs both the cap filtering above and rows lost to
    # concurrent SKIP LOCKED races, without needing a dedicated config knob for v1.
    candidate_window = max(batch * 4, 50)
    async with acquire_queue(None) as conn, conn.transaction():
        rows = await conn.fetch(
            f'WITH running_counts AS ( '
            f'  SELECT client_id, count(*) AS n FROM "{s}".di_job '
            "   WHERE status = 'running' AND kind = ANY($3) GROUP BY client_id"
            f'), ranked AS ( '
            f'  SELECT j.id, j.client_id, j.priority, j.run_after, j.created_at, '
            "         row_number() OVER (PARTITION BY j.client_id "
            "                            ORDER BY j.priority, j.run_after, j.created_at, j.id) AS rn "
            f'  FROM "{s}".di_job j '
            "   WHERE j.status = 'queued' AND j.run_after <= now() AND j.kind = ANY($3)"
            f'), eligible AS ( '
            "  SELECT ranked.id FROM ranked "
            "  LEFT JOIN running_counts rc ON rc.client_id = ranked.client_id "
            "  WHERE ranked.rn <= GREATEST($4 - COALESCE(rc.n, 0), 0) "
            "  ORDER BY ranked.priority, ranked.run_after, ranked.created_at, ranked.id "
            "  LIMIT $5"
            f') '
            f'UPDATE "{s}".di_job j SET status=\'running\', locked_by=$1, '
            " attempts = attempts + 1, lease_expires_at = now() + $2 * interval '1 second', "
            " stage = COALESCE(stage, 'claimed'), updated_at = now() "
            "FROM (SELECT id FROM \"" + s + "\".di_job "
            "      WHERE id IN (SELECT id FROM eligible) AND status = 'queued' "
            "      FOR UPDATE SKIP LOCKED LIMIT $6) c "
            f"WHERE j.id = c.id RETURNING {_CLAIM_COLS_QUALIFIED}",
            worker_id, settings.job_lease_seconds, list(kinds),
            settings.ingest_tenant_max_running, candidate_window, batch,
        )
    return [_row_to_claimed(r) for r in rows]


async def heartbeat(worker_id: str, job_ids: list[str]) -> list[str]:
    """Renew the lease for the given jobs. Returns the ids actually renewed (fenced on
    ``locked_by`` + ``status='running'``) — any id missing from the result was reclaimed
    elsewhere; the caller must cancel that job's local task immediately."""
    if not job_ids:
        return []
    settings = get_settings()
    s = _schema()
    async with acquire_queue(None) as conn:
        rows = await conn.fetch(
            f'UPDATE "{s}".di_job SET lease_expires_at = now() + $1 * interval \'1 second\' '
            "WHERE id = ANY($2::uuid[]) AND locked_by = $3 AND status = 'running' "
            "RETURNING id",
            settings.job_lease_seconds, [uuid.UUID(j) for j in job_ids], worker_id,
        )
    return [str(r["id"]) for r in rows]


async def complete(worker_id: str, job_id: str, status: JobStatus, *, error: str | None = None,
                   doc_id: str | None = None, version_id: str | None = None) -> bool:
    """Terminal transition, fenced on ``locked_by`` + ``status='running'``.

    Returns ``False`` (log-and-discard territory for the caller) when the fence did not match —
    a reclaimed attempt finished elsewhere and this write must not resurrect it.
    """
    s = _schema()
    async with acquire_queue(None) as conn:
        tag = await conn.execute(
            f'UPDATE "{s}".di_job SET '
            "status = $3, error = COALESCE($4, error), doc_id = COALESCE($5::uuid, doc_id), "
            "version_id = COALESCE($6::uuid, version_id), lease_expires_at = NULL, "
            "locked_by = NULL, finished_at = now(), updated_at = now() "
            "WHERE id = $1 AND locked_by = $2 AND status = 'running'",
            uuid.UUID(job_id), worker_id, status.value, error, _as_uuid(doc_id),
            _as_uuid(version_id),
        )
    return _rowcount(tag) > 0


async def release(worker_id: str, job_ids: list[str]) -> int:
    """Voluntary requeue on graceful shutdown: back to 'queued', ``run_after=now()``,
    ``attempts -= 1`` (a clean drain is not a failure), lock fields cleared."""
    if not job_ids:
        return 0
    s = _schema()
    async with acquire_queue(None) as conn:
        tag = await conn.execute(
            f'UPDATE "{s}".di_job SET status = \'queued\', locked_by = NULL, '
            "lease_expires_at = NULL, run_after = now(), attempts = GREATEST(attempts - 1, 0), "
            "updated_at = now() "
            "WHERE id = ANY($1::uuid[]) AND locked_by = $2 AND status = 'running'",
            [uuid.UUID(j) for j in job_ids], worker_id,
        )
    return _rowcount(tag)


async def retry_with_backoff(worker_id: str, job_id: str, *, error: str, backoff_base: float,
                             backoff_cap: float) -> bool:
    """Requeue a job after an in-process (non-fatal, retryable) pipeline failure, fenced on
    ``locked_by`` + ``status='running'``.

    Unlike :func:`release`, this counts as a used attempt (already incremented at claim time) —
    only a graceful drain or lease-expiry reclaim leaves the attempt uncharged. Proactively
    scheduling the backoff here (rather than waiting for the reaper's lease-expiry path) means a
    transient OCR/LLM/gateway failure retries within seconds, not minutes.
    """
    s = _schema()
    async with acquire_queue(None) as conn:
        tag = await conn.execute(
            f'UPDATE "{s}".di_job SET status=\'queued\', locked_by=NULL, lease_expires_at=NULL, '
            " error = $3, "
            " run_after = now() + least($4, $5 * 2 ^ attempts) * (0.5 + random()) * "
            "   interval '1 second', "
            " updated_at = now() "
            "WHERE id = $1 AND locked_by = $2 AND status = 'running'",
            uuid.UUID(job_id), worker_id, error, backoff_cap, backoff_base,
        )
    return _rowcount(tag) > 0


async def reap(*, backoff_base: float, backoff_cap: float) -> tuple[list[str], list[str]]:
    """Requeue jobs whose lease has lapsed (with exponential backoff + jitter), and dead-letter
    those that have exhausted ``max_attempts``. Also rescues pre-010 rows orphaned 'running' with
    a NULL lease by old-world crashes — those are handled distinctly by the caller (their payload
    is '{}' and their bytes died with the old process; see :mod:`di.worker`).

    Terminal taxonomy is unified: attempts-exhausted ALWAYS ends 'dead' here (never 'failed') so
    the page-worthy ``di_jobs_dead_total`` alert never misses a poison document that happens to
    fail via lease expiry rather than a clean in-process exception.

    Returns:
        ``(requeued_kinds, dead_kinds)`` — the ``kind`` of each affected row, for per-kind metrics.
    """
    s = _schema()
    async with acquire_queue(None) as conn:
        requeued_rows = await conn.fetch(
            f'UPDATE "{s}".di_job SET status=\'queued\', locked_by=NULL, lease_expires_at=NULL, '
            " run_after = now() + least($1, $2 * 2 ^ attempts) * (0.5 + random()) * "
            "   interval '1 second', "
            " updated_at = now() "
            "WHERE status='running' AND attempts < max_attempts "
            "  AND (lease_expires_at < now() "
            "       OR (lease_expires_at IS NULL AND updated_at < now() - interval '15 minutes')) "
            "RETURNING kind",
            backoff_cap, backoff_base,
        )
        dead_rows = await conn.fetch(
            f'UPDATE "{s}".di_job SET status=\'dead\', '
            " error = coalesce(error, 'lease expired; max attempts reached'), "
            " locked_by=NULL, lease_expires_at=NULL, finished_at=now(), updated_at=now() "
            "WHERE status='running' AND attempts >= max_attempts "
            "  AND (lease_expires_at < now() "
            "       OR (lease_expires_at IS NULL AND updated_at < now() - interval '15 minutes')) "
            "RETURNING kind",
        )
    return [r["kind"] for r in requeued_rows], [r["kind"] for r in dead_rows]


async def queue_stats() -> list[dict[str, Any]]:
    """Per ``(kind, status)`` counts + oldest-queued age, for the queue-depth/age gauges. Cheap:
    rides the partial ``di_job_claim``/status indexes regardless of table size."""
    s = _schema()
    async with acquire_queue(None) as conn:
        rows = await conn.fetch(
            f'SELECT kind, status, count(*) AS n, '
            f'       extract(epoch FROM now() - min(created_at)) AS oldest_age_seconds '
            f'FROM "{s}".di_job GROUP BY kind, status'
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Admin operations — retry / cancel (tenant-scoped, via the normal runtime pool)
# ---------------------------------------------------------------------------
async def retry(client_id: str, job_id: str) -> Job | None:
    """Requeue a dead-lettered job for another attempt (resets attempts to 0 and clears the
    error). Returns ``None`` if the job does not exist for this tenant or is not 'dead'."""
    s = _schema()
    async with acquire(client_id) as conn:
        row = await conn.fetchrow(
            f'UPDATE "{s}".di_job SET status = \'queued\', attempts = 0, error = NULL, '
            " run_after = now(), locked_by = NULL, lease_expires_at = NULL, "
            " finished_at = NULL, updated_at = now() "
            "WHERE client_id = $1 AND id = $2 AND status = 'dead' "
            f"RETURNING {_JOB_COLS}",
            client_id, uuid.UUID(job_id),
        )
    return _row_to_job(row) if row else None


async def cancel(client_id: str, job_id: str) -> Job | None:
    """Cancel a job that has not started yet (``queued`` -> ``canceled`` only — a running job
    must finish or be reclaimed by the reaper; there is no preemption). Returns ``None`` if the
    job does not exist for this tenant or is not 'queued'."""
    s = _schema()
    async with acquire(client_id) as conn:
        row = await conn.fetchrow(
            f'UPDATE "{s}".di_job SET status = \'canceled\', finished_at = now(), '
            " updated_at = now() "
            "WHERE client_id = $1 AND id = $2 AND status = 'queued' "
            f"RETURNING {_JOB_COLS}",
            client_id, uuid.UUID(job_id),
        )
    return _row_to_job(row) if row else None


# ---------------------------------------------------------------------------
# Pipeline-side writes (fenced when driven by a worker)
# ---------------------------------------------------------------------------
async def append_event(client_id: str, job_id: str, event: JobEvent, *,
                       locked_by: str | None = None) -> bool:
    """Append one stage event to a job, atomically (server-side concatenation, so concurrent
    stages appending at the same instant cannot clobber each other's events).

    Args:
        locked_by: When given, the write is FENCED — it only applies if the job is still
            'running' and locked by this worker. A zombie worker whose job was reclaimed writes
            zero rows and gets ``False`` back, signaling it must stop immediately. ``None`` skips
            fencing (non-worker callers).

    Returns:
        Whether the write actually applied.
    """
    s = _schema()
    conds = "client_id = $1 AND id = $2"
    params: list[Any] = [client_id, uuid.UUID(job_id), [event.model_dump(mode="json")]]
    if locked_by is not None:
        conds += " AND locked_by = $4 AND status = 'running'"
        params.append(locked_by)
    async with acquire(client_id) as conn:
        tag = await conn.execute(
            f'UPDATE "{s}".di_job SET events = events || $3::jsonb, updated_at = now() '
            f"WHERE {conds}",
            *params,
        )
    return _rowcount(tag) > 0


async def set_status(client_id: str, job_id: str, status: JobStatus, *, stage: str | None = None,
                     error: str | None = None, doc_id: str | None = None,
                     version_id: str | None = None, locked_by: str | None = None) -> bool:
    """Transition a job's status, optionally recording its stage/error/outputs.

    Optional arguments use COALESCE semantics: passing None leaves the stored value untouched.
    ``finished_at`` is stamped with ``now()`` on a terminal status and cleared otherwise.

    Args:
        locked_by: Fences the write exactly like :func:`append_event` — see there for why.

    Returns:
        Whether the write actually applied.
    """
    s = _schema()
    conds = "client_id = $1 AND id = $2"
    params: list[Any] = [
        client_id, uuid.UUID(job_id), status.value, stage, error, _as_uuid(doc_id),
        _as_uuid(version_id), status in _TERMINAL,
    ]
    if locked_by is not None:
        conds += " AND locked_by = $9 AND status = 'running'"
        params.append(locked_by)
    async with acquire(client_id) as conn:
        tag = await conn.execute(
            f'UPDATE "{s}".di_job SET '
            "status = $3, "
            "stage = COALESCE($4, stage), "
            "error = COALESCE($5, error), "
            "doc_id = COALESCE($6::uuid, doc_id), "
            "version_id = COALESCE($7::uuid, version_id), "
            "finished_at = CASE WHEN $8::boolean THEN now() ELSE NULL END, "
            "updated_at = now() "
            f"WHERE {conds}",
            *params,
        )
    return _rowcount(tag) > 0


async def count_active_and_today(client_id: str) -> tuple[int, int]:
    """Count a tenant's active jobs and jobs created since local midnight — the two numbers the
    ingest admission quota checks.

    Args:
        client_id: The owning tenant. Runs under ``acquire(client_id)`` — FORCE RLS means an
            unbound or wrongly-scoped caller would silently see zero rows and the quota would
            never trip in production, so this MUST be called with the tenant GUC bound.

    Returns:
        ``(active_count, today_count)``.
    """
    s = _schema()
    async with acquire(client_id) as conn:
        active = await conn.fetchval(
            f'SELECT count(*) FROM "{s}".di_job WHERE client_id = $1 AND status = ANY($2)',
            client_id, list(_ACTIVE_STATUSES),
        )
        today = await conn.fetchval(
            f'SELECT count(*) FROM "{s}".di_job '
            "WHERE client_id = $1 AND created_at >= date_trunc('day', now())",
            client_id,
        )
    return int(active or 0), int(today or 0)


async def purge_client_jobs(client_id: str) -> int:
    """Delete every job belonging to a tenant (right-to-erasure / offboarding).

    A worker mid-execution on a since-deleted job does not need special cancellation here: its
    next fenced write (heartbeat/append_event/complete) affects zero rows (the row is gone, not
    merely reassigned) and the worker treats that exactly like a reclaimed job — abandon
    immediately. Job payload blobs are swept by the caller's ``BlobStore.delete_client()`` (already
    part of the admin purge flow), since blob-at-accept objects live under the same
    tenant-prefixed, content-addressed key scheme as every other blob — no separate sweep needed.
    """
    s = _schema()
    async with acquire(client_id) as conn:
        tag = await conn.execute(f'DELETE FROM "{s}".di_job WHERE client_id = $1', client_id)
    return _rowcount(tag)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def get_job(client_id: str, job_id: str) -> Job | None:
    """Fetch a single job.

    A malformed id is treated as "not found" rather than an error, so callers can map it straight
    to a 404.
    """
    s = _schema()
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        return None
    async with acquire(client_id) as conn:
        row = await conn.fetchrow(
            f'SELECT {_JOB_COLS} FROM "{s}".di_job WHERE client_id = $1 AND id = $2',
            client_id, job_uuid,
        )
    return _row_to_job(row) if row else None


async def find_by_idempotency(client_id: str, idempotency_key: str) -> Job | None:
    s = _schema()
    async with acquire(client_id) as conn:
        row = await conn.fetchrow(
            f'SELECT {_JOB_COLS} FROM "{s}".di_job '
            "WHERE client_id = $1 AND idempotency_key = $2",
            client_id, idempotency_key,
        )
    return _row_to_job(row) if row else None


async def list_jobs(client_id: str, *, limit: int = 50, cursor: str | None = None,
                    status: JobStatus | None = None) -> tuple[list[Job], str | None]:
    """List a tenant's jobs, newest first, with keyset pagination.

    Ordering is ``(created_at DESC, id DESC)`` and paging compares the row value
    ``(created_at, id)`` against the cursor position. One extra row is fetched to decide whether a
    further page exists, so ``next_cursor`` is None on the last page.

    Raises:
        ValueError: If ``cursor`` is malformed.
    """
    s = _schema()
    page = _clamp_limit(limit)
    conds = ["client_id = $1"]
    params: list[Any] = [client_id]
    if status is not None:
        params.append(status.value)
        conds.append(f"status = ${len(params)}")
    if cursor:
        created_at, cursor_id = _decode_cursor(cursor)
        params.append(created_at)
        params.append(uuid.UUID(cursor_id))
        conds.append(
            f"(created_at, id) < (${len(params) - 1}::timestamptz, ${len(params)}::uuid)"
        )
    sql = (f'SELECT {_JOB_COLS} FROM "{s}".di_job WHERE ' + " AND ".join(conds)
           + f" ORDER BY created_at DESC, id DESC LIMIT {page + 1}")
    async with acquire(client_id) as conn:
        rows = await conn.fetch(sql, *params)

    has_more = len(rows) > page
    jobs = [_row_to_job(r) for r in rows[:page]]
    next_cursor = _encode_cursor(jobs[-1].created_at, jobs[-1].id) if has_more and jobs else None
    return jobs, next_cursor


def worker_id() -> str:
    """Generate a unique worker identity: hostname/pid-ish prefix + random suffix, stable for the
    life of the process. Used as ``locked_by`` — the entire fencing scheme depends on this being
    unique across restarts, so a random suffix is included even though pid is usually enough."""
    import os
    import socket

    return f"{socket.gethostname()}-{os.getpid()}-{random.randint(0, 0xFFFFFF):06x}"
