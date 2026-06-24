"""Ingest router — `POST /api/v1/ingest` (multipart upload, SSE stage stream).

Drives di.pipeline.ingest_document and re-streams its IngestEvent stages to the caller as SSE.
"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile
from sse_starlette.sse import EventSourceResponse

from di.pipeline import ingest_document

router = APIRouter(prefix="/api/v1", tags=["ingest"])


@router.post("/ingest")
async def ingest(
    # FastAPI requires File()/Form() in parameter defaults (B008 is a false positive here)
    client_id: str = Form(...),  # noqa: B008
    file: UploadFile = File(...),  # noqa: B008
) -> EventSourceResponse:
    content = await file.read()
    filename = file.filename or "upload"
    mime = file.content_type

    async def _stream():
        async for event in ingest_document(client_id, content, filename, mime=mime):
            yield {"event": "stage", "data": event.model_dump_json()}

    return EventSourceResponse(_stream())


@router.get("/ingest/health")
async def ingest_health() -> dict[str, str]:
    return {"status": "ok", "router": "ingest"}
