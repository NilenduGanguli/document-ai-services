"""Read-side access audit: "who read this client's data, and did they see it masked?"

Two independent records of every tenant-scoped request:

1. A structured log line (``logging.getLogger("di.access")``), emitted **synchronously in the
   middleware before the response returns** — this is the crash-safe record. It reaches stdout
   (captured by the platform's log pipeline) even if the process is SIGKILLed a moment later.
2. A row in ``di_access_log``, written by a batched background task — the queryable record, used
   by the admin ``GET /access-log`` endpoint to answer "who accessed client X's data".

The DB row is best-effort by construction (batched, async); the log line is not. When
``access_audit_strict`` is true (required in production — see ``di.posture``), a saturated queue
fails the request with 503 rather than silently dropping the DB-side record: "no audit -> no
reads" is a deliberate, bank-approved trade-off, not a bug.

Partition management is entirely out of this module's hands: ``di/db.py``'s
``_ensure_access_log_partitions`` creates months of headroom under the owner role at migration
time. This writer never issues DDL — a missing partition just fails that batch and is surfaced via
the ``audit`` readiness component, not silently retried with app-level DDL the runtime role
cannot perform anyway.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from di.config import get_settings
from di.db import acquire

logger = logging.getLogger(__name__)
_access_logger = logging.getLogger("di.access")


@dataclass
class AccessRecord:
    method: str
    route: str
    status: int
    ts: float = field(default_factory=time.time)
    key_id: str | None = None
    principal: str | None = None
    client_id: str | None = None
    masked: bool | None = None
    request_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_log_line(self) -> str:
        return json.dumps({
            "ts": self.ts, "key_id": self.key_id, "principal": self.principal,
            "client_id": self.client_id, "method": self.method, "route": self.route,
            "status": self.status, "masked": self.masked, "request_id": self.request_id,
            **self.extra,
        }, default=str)


class AuditUnavailable(Exception):
    """Raised (strict mode only) when the access-log queue is saturated — the caller must 503
    rather than serve a tenant-data read with no durable audit record."""


class AccessLogWriter:
    """Background batched writer for ``di_access_log``. One instance per process, owned by the
    app lifespan."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[AccessRecord] | None = None
        self._task: asyncio.Task[None] | None = None
        self._dropped = 0
        self._healthy = True
        self._last_error: str | None = None

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def dropped_count(self) -> int:
        return self._dropped

    def start(self) -> None:
        settings = get_settings()
        self._queue = asyncio.Queue(maxsize=settings.access_audit_queue_max)
        self._task = asyncio.create_task(self._run(), name="access-log-writer")

    async def stop(self, timeout: float = 10.0) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except (asyncio.CancelledError, TimeoutError, Exception):  # noqa: BLE001 - best-effort drain
            pass
        self._task = None

    async def enqueue(self, record: AccessRecord) -> None:
        """Queue a record for the background writer.

        Raises:
            AuditUnavailable: in strict mode, when the queue is full — the caller must 503.
        """
        settings = get_settings()
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._dropped += 1
            logger.warning("access-log queue full; %s dropped so far", self._dropped)
            if settings.access_audit_strict:
                raise AuditUnavailable("access-log queue saturated") from None

    async def _run(self) -> None:
        settings = get_settings()
        assert self._queue is not None
        batch: list[AccessRecord] = []
        while True:
            try:
                timeout = settings.access_audit_flush_ms / 1000.0
                try:
                    record = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                    batch.append(record)
                except TimeoutError:
                    pass
                while len(batch) < settings.access_audit_batch:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if batch:
                    await self._flush(batch)
                    batch = []
            except asyncio.CancelledError:
                if batch:
                    await self._flush(batch)
                raise
            except Exception:  # noqa: BLE001 - the writer must never die from one bad batch
                logger.exception("access-log writer iteration failed")
                self._healthy = False
                self._last_error = "writer loop error"
                batch = []
                await asyncio.sleep(1.0)

    async def _flush(self, batch: list[AccessRecord]) -> None:
        schema = get_settings().pg_schema
        try:
            async with acquire(None) as conn:
                await conn.executemany(
                    f'INSERT INTO "{schema}".di_access_log '
                    "(ts, key_id, principal, client_id, method, route, status, masked, "
                    " request_id, extra) "
                    "VALUES (to_timestamp($1), $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                    [
                        (r.ts, r.key_id, r.principal, r.client_id, r.method, r.route, r.status,
                         r.masked, r.request_id, r.extra)
                        for r in batch
                    ],
                )
            self._healthy = True
            self._last_error = None
        except Exception as exc:  # noqa: BLE001 - missing partition, DB blip, etc.
            self._healthy = False
            self._last_error = str(exc)
            logger.error("access-log flush failed for %d record(s): %s", len(batch), exc)


# Module-level singleton, matching di.ingest_runner's pattern.
_writer: AccessLogWriter | None = None


def start_writer() -> None:
    global _writer
    _writer = AccessLogWriter()
    _writer.start()


async def stop_writer() -> None:
    global _writer
    if _writer is not None:
        await _writer.stop()
        _writer = None


def writer_health() -> tuple[bool, str | None, int]:
    if _writer is None:
        return False, "writer not started", 0
    return _writer.healthy, _writer.last_error, _writer.dropped_count


async def record_access(record: AccessRecord) -> None:
    """Dual-emit: synchronous structured log line (crash-safe), then queue the DB row.

    Args:
        record: The access to record.

    Raises:
        AuditUnavailable: in strict mode, when the queue is saturated.
    """
    _access_logger.info(record.as_log_line())
    if _writer is not None:
        await _writer.enqueue(record)


def resolve_audit_client_id(path_params: dict[str, Any], query_params: Any,
                            state_client_id: str | None) -> str | None:
    """Tenant resolution order for the audit middleware: path param, then query param, then
    ``request.state.audit_client_id`` (set by handlers whose client_id arrives in the body, e.g.
    the multipart ingest form).

    Pure and unit-testable: takes plain dict-likes rather than a live ``Request``.
    """
    client_id = path_params.get("client_id")
    if client_id:
        return str(client_id)
    client_id = query_params.get("client_id") if query_params is not None else None
    if client_id:
        return str(client_id)
    return str(state_client_id) if state_client_id else None
