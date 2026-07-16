"""Durable, DB-backed job store for asynchronous ingestion.

Ingestion used to be a single synchronous SSE stream: a dropped connection lost the work and
left the caller with no handle to retry against. This module backs each ingest run with a row in
``di_job`` (created by migration ``005``), so the API can answer ``202 + job_id`` immediately and
callers can poll or re-attach for stage events at any time.

Design notes:
- every access goes through ``di.db.acquire(client_id)``, binding the RLS tenant GUC — ``di_job``
  is tenant-scoped;
- ``append_event`` concatenates server-side (``events = events || $x::jsonb``) rather than doing a
  read-modify-write, so concurrent pipeline stages cannot clobber one another's events;
- ``list_jobs`` uses keyset (not OFFSET) pagination over ``(created_at DESC, id DESC)``, so pages
  stay stable and cheap while new jobs are being inserted at the head of the feed.
"""
from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import asyncpg
from pydantic import BaseModel, Field

from di.config import get_settings
from di.db import acquire

__all__ = [
    "Job",
    "JobEvent",
    "JobStatus",
    "append_event",
    "count_active_and_today",
    "create_job",
    "find_by_idempotency",
    "get_job",
    "list_jobs",
    "purge_client_jobs",
    "set_status",
]

#: Page-size guard rails for :func:`list_jobs`.
_MIN_LIMIT = 1
_MAX_LIMIT = 200

#: Selected column list — explicit (not ``*``) so row -> model mapping is stable.
_JOB_COLS = (
    "id, client_id, status, stage, document_name, doc_id, version_id, error, "
    "idempotency_key, events, created_at, updated_at, finished_at"
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


#: Statuses after which no further work happens; these stamp ``finished_at``.
_TERMINAL = frozenset({JobStatus.succeeded, JobStatus.failed})


class JobEvent(BaseModel):
    """One durable pipeline stage event (the persisted analogue of ``IngestEvent``)."""

    stage: str
    status: str = "ok"
    detail: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class Job(BaseModel):
    """An ingestion job and its accumulated stage events."""

    id: str
    client_id: str
    status: JobStatus
    stage: str | None = None
    document_name: str | None = None
    doc_id: str | None = None
    version_id: str | None = None
    error: str | None = None
    idempotency_key: str | None = None
    events: list[JobEvent] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _schema() -> str:
    return get_settings().pg_schema


def _as_uuid(value: str | None) -> uuid.UUID | None:
    """Coerce an optional id string to a UUID.

    Args:
        value: A UUID string, or None/empty.

    Returns:
        The parsed UUID, or None when no value was supplied.

    Raises:
        ValueError: If ``value`` is a non-empty string that is not a valid UUID.
    """
    return uuid.UUID(value) if value else None


def _clamp_limit(limit: int) -> int:
    """Clamp a caller-supplied page size into ``[1, 200]``.

    Args:
        limit: The requested page size, possibly out of range.

    Returns:
        The page size clamped into the supported range.
    """
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


def _encode_cursor(created_at: datetime, job_id: str) -> str:
    """Encode a keyset position into an opaque, URL-safe cursor.

    Args:
        created_at: The ``created_at`` of the last row on the page.
        job_id: The id of the last row on the page (tie-breaker).

    Returns:
        An unpadded, URL-safe base64 encoding of ``"<created_at>|<id>"``.
    """
    raw = f"{created_at.isoformat()}|{job_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode an opaque cursor produced by :func:`_encode_cursor`.

    Cursors arrive from clients and are therefore untrusted: every malformed shape (bad base64,
    non-UTF-8 bytes, missing separator, unparseable timestamp, non-UUID id) raises ``ValueError``
    so callers can map it to a 400 rather than leaking a driver error.

    Args:
        cursor: The opaque cursor string.

    Returns:
        A ``(created_at, job_id)`` keyset position.

    Raises:
        ValueError: If the cursor is malformed in any way.
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
    """Map a ``di_job`` row to a :class:`Job` (uuid -> str, jsonb -> JobEvent list)."""
    return Job(
        id=str(row["id"]),
        client_id=row["client_id"],
        status=JobStatus(row["status"]),
        stage=row["stage"],
        document_name=row["document_name"],
        doc_id=str(row["doc_id"]) if row["doc_id"] else None,
        version_id=str(row["version_id"]) if row["version_id"] else None,
        error=row["error"],
        idempotency_key=row["idempotency_key"],
        events=[JobEvent.model_validate(e) for e in (row["events"] or [])],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
async def create_job(*, client_id: str, document_name: str | None,
                     idempotency_key: str | None = None) -> Job:
    """Create a ``queued`` job.

    When ``idempotency_key`` is supplied and a job already exists for it, the existing job is
    returned instead of raising — the unique index on ``(client_id, idempotency_key)`` makes this
    safe under concurrent retries of the same request.

    Args:
        client_id: The owning tenant.
        document_name: The uploaded document's name, if known.
        idempotency_key: Caller-supplied key to collapse duplicate submissions.

    Returns:
        The newly created job, or the pre-existing job with the same idempotency key.
    """
    s = _schema()
    try:
        async with acquire(client_id) as conn:
            row = await conn.fetchrow(
                f'INSERT INTO "{s}".di_job '
                "(id, client_id, status, document_name, idempotency_key) "
                f"VALUES ($1,$2,$3,$4,$5) RETURNING {_JOB_COLS}",
                str(uuid.uuid4()), client_id, JobStatus.queued.value, document_name,
                idempotency_key,
            )
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


async def append_event(client_id: str, job_id: str, event: JobEvent) -> None:
    """Append one stage event to a job, atomically.

    The append happens server-side (``events = events || $3::jsonb``) rather than as a
    read-modify-write, so concurrent stages appending at the same instant cannot clobber each
    other's events.

    Args:
        client_id: The owning tenant.
        job_id: The job's id.
        event: The event to append.

    Raises:
        ValueError: If ``job_id`` is not a valid UUID.
    """
    s = _schema()
    async with acquire(client_id) as conn:
        await conn.execute(
            f'UPDATE "{s}".di_job SET events = events || $3::jsonb, updated_at = now() '
            "WHERE client_id = $1 AND id = $2",
            client_id, uuid.UUID(job_id), [event.model_dump(mode="json")],
        )


async def set_status(client_id: str, job_id: str, status: JobStatus, *, stage: str | None = None,
                     error: str | None = None, doc_id: str | None = None,
                     version_id: str | None = None) -> None:
    """Transition a job's status, optionally recording its stage/error/outputs.

    Optional arguments use COALESCE semantics: passing None leaves the stored value untouched.
    ``finished_at`` is stamped with ``now()`` on a terminal status (``succeeded``/``failed``) and
    cleared otherwise, so it is non-null exactly when the job is finished (a failed job that is
    retried back to ``running`` clears it again).

    Args:
        client_id: The owning tenant.
        job_id: The job's id.
        status: The new status.
        stage: The stage the job reached, if it changed.
        error: The failure message, when transitioning to ``failed``.
        doc_id: The resulting document id, once known.
        version_id: The resulting version id, once known.

    Raises:
        ValueError: If ``job_id``, ``doc_id`` or ``version_id`` is not a valid UUID.
    """
    s = _schema()
    async with acquire(client_id) as conn:
        await conn.execute(
            f'UPDATE "{s}".di_job SET '
            "status = $3, "
            "stage = COALESCE($4, stage), "
            "error = COALESCE($5, error), "
            "doc_id = COALESCE($6::uuid, doc_id), "
            "version_id = COALESCE($7::uuid, version_id), "
            "finished_at = CASE WHEN $8::boolean THEN now() ELSE NULL END, "
            "updated_at = now() "
            "WHERE client_id = $1 AND id = $2",
            client_id, uuid.UUID(job_id), status.value, stage, error, _as_uuid(doc_id),
            _as_uuid(version_id), status in _TERMINAL,
        )


#: Non-terminal statuses, derived from the enum rather than hardcoded — when a later phase adds
#: e.g. ``queued``/``dead``/``canceled`` states, "active" tracks them automatically instead of
#: silently under-counting against a stale literal list.
_ACTIVE_STATUSES = tuple(s.value for s in JobStatus if s not in _TERMINAL)


async def count_active_and_today(client_id: str) -> tuple[int, int]:
    """Count a tenant's active jobs and jobs created since local midnight — the two numbers the
    ingest admission quota checks.

    Two separate scalar subqueries (not one query with two ``FILTER`` clauses) so each rides its
    own index (``di_job_client_status`` for the active count, ``di_job_client_created`` for
    today's count) rather than forcing one plan to serve both.

    Args:
        client_id: The owning tenant. Runs under ``acquire(client_id)`` — FORCE RLS means an
            unbound or wrongly-scoped caller would silently see zero rows and the quota would
            never trip, so this MUST be called with the tenant GUC bound.

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
    """Delete every job belonging to a tenant.

    Args:
        client_id: The owning tenant.

    Returns:
        The number of job rows deleted.
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

    Args:
        client_id: The owning tenant.
        job_id: The job's id. A malformed id is treated as "not found" rather than an error, so
            callers can map it straight to a 404.

    Returns:
        The job, or None if it does not exist for this tenant.
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
    """Look up a job by its caller-supplied idempotency key.

    Args:
        client_id: The owning tenant.
        idempotency_key: The key supplied at creation.

    Returns:
        The matching job, or None if the key is unused for this tenant.
    """
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
    ``(created_at, id)`` against the cursor position — stable and index-friendly even as new jobs
    are inserted at the head of the feed. One extra row is fetched to decide whether a further
    page exists, so ``next_cursor`` is None on the last page.

    Args:
        client_id: The owning tenant.
        limit: Page size; clamped into ``[1, 200]``.
        cursor: An opaque cursor from a previous call, or None to start at the newest job.
        status: Restrict the feed to a single status.

    Returns:
        A ``(jobs, next_cursor)`` tuple; ``next_cursor`` is None when the page is the last one.

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
