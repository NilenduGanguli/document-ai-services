"""Ingest router.

``POST /api/v1/ingest`` accepts a document and returns **202 + job_id** (the durable default):
the pipeline runs in the background and the caller polls ``GET /api/v1/jobs/{id}``, so a dropped
connection, an LB timeout or a pod restart no longer loses the work. ``?stream=true`` keeps the
original SSE behaviour for interactive callers who want live stages on one connection.

Admission order (the one place fairness/backpressure is enforced): ``authorize_client`` ->
idempotency pre-check (a retried already-accepted submit must never 429) -> unified per-tenant
quota. ``stream=true`` shares the quota via an in-process inflight-stream counter folded into the
active count, so a leaked ingest-scoped key cannot bypass the quota by using the streaming path.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from di import jobs
from di.auth import Principal, authorize_client, require_scope
from di.config import get_settings
from di.ingest_runner import submit_ingest_job
from di.pipeline import ingest_document

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["ingest"])

#: In-process count of currently-open ?stream=true requests per tenant. Streaming ingests never
#: create a di_job row, so without this they would be invisible to the DB-backed active count and
#: a leaked key could saturate the pipeline via stream=true exactly as before the quota existed.
#: Per-process like the rate limiter — documented, not a precision control.
_inflight_streams: dict[str, int] = defaultdict(int)


class IngestAccepted(BaseModel):
    """202 response: a durable handle the caller can poll, retry against, and resume."""

    job_id: str
    client_id: str
    status: str
    document_name: str | None = None
    reused: bool = False        # true when an idempotency_key matched an existing job


async def _read_upload(file: UploadFile) -> bytes:
    """Read the upload, enforcing the configured size cap (unbounded reads are a DoS)."""
    settings = get_settings()
    content = await file.read()
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the {settings.max_upload_mb} MB limit "
                   f"({len(content) / 1024 / 1024:.1f} MB)",
        )
    if not content:
        raise HTTPException(status_code=400, detail="empty file")
    return content


async def _enforce_ingest_quota(client_id: str) -> None:
    """Admission-time fairness check: refuse a new ingest once the tenant's active-job count or
    daily count is at its limit. Runs under ``acquire(client_id)`` (via
    ``jobs.count_active_and_today``) — FORCE RLS means an unbound caller would silently see zero
    rows and the quota would never trip in production, so this must never be called without a
    resolved ``client_id``.

    Raises:
        HTTPException: 429 with ``Retry-After`` when either limit is met or exceeded.
    """
    settings = get_settings()
    policy = await _tenant_policy(client_id)
    max_active = (policy or {}).get("max_active_jobs")
    if max_active is None:
        max_active = settings.ingest_max_active_jobs_per_client

    # 0 is overloaded across the two levels this can come from: the fleet default's 0 means "no
    # daily cap" (di.config.Settings.ingest_daily_limit_per_client), but an explicit tenant-policy
    # override of 0 is a deliberate "block this tenant entirely" lever (see
    # TenantPolicyRequest.daily_ingest_limit's docstring). Only treat 0 as a block when it came
    # from an explicit override — a bare `if daily_limit` would silently never trip on either.
    policy_daily = (policy or {}).get("daily_ingest_limit")
    daily_limit = policy_daily if policy_daily is not None else settings.ingest_daily_limit_per_client
    daily_cap_active = policy_daily is not None or daily_limit > 0

    active, today = await jobs.count_active_and_today(client_id)
    active += _inflight_streams.get(client_id, 0)

    if max_active and active >= max_active:
        raise HTTPException(
            status_code=429,
            detail=f"tenant ingest quota exceeded: {active} active jobs >= limit {max_active}",
            headers={"Retry-After": "30"},
        )
    if daily_cap_active and today >= daily_limit:
        raise HTTPException(
            status_code=429,
            detail=f"tenant daily ingest quota exceeded: {today} today >= limit {daily_limit}",
            headers={"Retry-After": "3600"},
        )


async def _tenant_policy(client_id: str) -> dict | None:
    """Best-effort lookup of a per-tenant policy override. Never fails the request — a policy
    lookup error just falls back to the fleet-wide settings defaults."""
    try:
        from di import store
        return await store.fetch_tenant_policy(client_id)
    except Exception:  # noqa: BLE001 - quota admission must never break on a lookup hiccup
        logger.warning("tenant policy lookup failed for %s; using fleet defaults", client_id,
                       exc_info=True)
        return None


@router.post("/ingest", response_model=IngestAccepted, status_code=202)
async def ingest(
    request: Request,
    # FastAPI requires File()/Form() in parameter defaults (B008 is a false positive here)
    client_id: str = Form(...),  # noqa: B008
    file: UploadFile = File(...),  # noqa: B008
    external_document_id: str | None = Form(None),  # noqa: B008
    idempotency_key: str | None = Form(None),  # noqa: B008
    stream: bool = Query(False, description="Stream SSE stages instead of returning 202"),  # noqa: B008
    principal: Principal = Depends(require_scope("ingest")),  # noqa: B008
):
    """Accept a document for ingestion. Returns 202 + job_id, or an SSE stream when stream=true."""
    authorize_client(principal, client_id)
    # client_id arrives in the multipart form body, not a path/query param, so the audit
    # middleware's normal resolution (path -> query) would otherwise miss every ingest request.
    request.state.audit_client_id = client_id
    content = await _read_upload(file)
    filename = file.filename or "upload"
    mime = file.content_type

    if stream:
        # No di_job row exists for the streaming path, so it is invisible to the DB-backed active
        # count — fold it into the quota via the in-process inflight counter (module docstring).
        await _enforce_ingest_quota(client_id)
        _inflight_streams[client_id] += 1

        async def _stream():
            try:
                async for event in ingest_document(
                    client_id, content, filename, mime=mime,
                    external_document_id=external_document_id, created_by=principal.name,
                ):
                    yield {"event": "stage", "data": event.model_dump_json()}
            finally:
                _inflight_streams[client_id] -= 1
                if _inflight_streams[client_id] <= 0:
                    _inflight_streams.pop(client_id, None)

        return EventSourceResponse(_stream())

    # Idempotency BEFORE the quota check: a retry of an already-accepted submit must never 429.
    if idempotency_key:
        existing = await jobs.find_by_idempotency(client_id, idempotency_key)
        if existing is not None:
            return IngestAccepted(job_id=existing.id, client_id=client_id,
                                  status=existing.status.value,
                                  document_name=existing.document_name, reused=True)

    await _enforce_ingest_quota(client_id)

    job = await jobs.create_job(client_id=client_id, document_name=filename,
                                idempotency_key=idempotency_key)
    await submit_ingest_job(
        job_id=job.id, client_id=client_id, file_bytes=content, filename=filename, mime=mime,
        external_document_id=external_document_id, created_by=principal.name,
    )
    return IngestAccepted(job_id=job.id, client_id=client_id, status=job.status.value,
                          document_name=filename)


@router.get("/ingest/health")
async def ingest_health() -> dict[str, str]:
    return {"status": "ok", "router": "ingest"}
