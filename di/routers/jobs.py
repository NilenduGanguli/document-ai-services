"""Jobs router — the durable handle for an async ingest.

Consumers poll ``GET /api/v1/jobs/{job_id}`` for live stage progress and the terminal outcome,
instead of holding an SSE connection open for the life of the pipeline.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from di import jobs as jobs_mod
from di.auth import Principal, authorize_client, require_principal
from di.jobs import Job, JobStatus
from di.store import clamp_limit

router = APIRouter(prefix="/api/v1", tags=["jobs"])


class JobListResponse(BaseModel):
    client_id: str
    count: int
    jobs: list[Job]
    next_cursor: str | None = None


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    client_id: str,
    limit: int | None = Query(None, ge=1, le=200),  # noqa: B008
    cursor: str | None = None,
    status: JobStatus | None = None,
    principal: Principal = Depends(require_principal),  # noqa: B008
) -> JobListResponse:
    """List a client's ingest jobs, newest first (keyset pagination)."""
    authorize_client(principal, client_id)
    try:
        rows, next_cursor = await jobs_mod.list_jobs(
            client_id, limit=clamp_limit(limit), cursor=cursor, status=status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JobListResponse(client_id=client_id, count=len(rows), jobs=rows,
                           next_cursor=next_cursor)


@router.get("/jobs/{job_id}", response_model=Job)
async def get_job(
    job_id: str,
    client_id: str,
    principal: Principal = Depends(require_principal),  # noqa: B008
) -> Job:
    """Fetch one job: status, current stage, every recorded stage event, and any error."""
    authorize_client(principal, client_id)
    job = await jobs_mod.get_job(client_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job
