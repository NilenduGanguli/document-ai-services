"""Search router — `POST /api/v1/clients/{id}/search`. Placeholder; M2–M3.

Owns hybrid (dense + lexical + structural ltree) retrieval scoped to client/doc/section,
returning nodes + grounding. Uses di.subtree retrieval + retrieval-service rerank.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.get("/search/health")
async def search_health() -> dict[str, str]:
    return {"status": "ok", "router": "search"}
