"""Ingest router — `POST /api/v1/ingest` (SSE). Placeholder; implemented in M1–M4.

Owns: the multipart upload endpoint + SSE bridge that drives di.pipeline.ingest_document and
re-streams its IngestEvent stages to the caller.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["ingest"])


@router.get("/ingest/health")
async def ingest_health() -> dict[str, str]:
    return {"status": "ok", "router": "ingest"}
