"""Ingest router.

``POST /api/v1/ingest`` accepts a document and returns **202 + job_id** (the durable default):
the pipeline runs in the background and the caller polls ``GET /api/v1/jobs/{id}``, so a dropped
connection, an LB timeout or a pod restart no longer loses the work. ``?stream=true`` keeps the
original SSE behaviour for interactive callers who want live stages on one connection.
"""
from __future__ import annotations

import logging

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
    content = await _read_upload(file)
    filename = file.filename or "upload"
    mime = file.content_type

    if stream:
        async def _stream():
            async for event in ingest_document(
                client_id, content, filename, mime=mime,
                external_document_id=external_document_id, created_by=principal.name,
            ):
                yield {"event": "stage", "data": event.model_dump_json()}

        return EventSourceResponse(_stream())

    if idempotency_key:
        existing = await jobs.find_by_idempotency(client_id, idempotency_key)
        if existing is not None:
            return IngestAccepted(job_id=existing.id, client_id=client_id,
                                  status=existing.status.value,
                                  document_name=existing.document_name, reused=True)

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
