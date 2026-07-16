"""Search router — hybrid (dense + lexical + structural) retrieval scoped to a client.

Embeds the query through the retrieval gateway (when pgvector is available), runs the
index-many/return-parent hybrid search, and returns ranked knode hits with grounding. ``top_k`` is
bounded and masking follows the server-side policy by default.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from di import observability, serving, store
from di.auth import Principal, authorize_client, require_scope
from di.config import get_settings
from di.db import pgvector_available
from di.retrieval_client import get_retrieval_client

router = APIRouter(prefix="/api/v1", tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    scope_path: str | None = None
    doc_id: str | None = None
    top_k: int = Field(20, ge=1, le=100)
    current_only: bool = True
    mask: bool | None = None


class SearchResponse(BaseModel):
    client_id: str
    query: str
    count: int
    masked: bool
    vector: bool = Field(..., description="False when pgvector is absent (lexical-only results)")
    hits: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/clients/{client_id}/search", response_model=SearchResponse)
async def search(
    request: Request,
    client_id: str, req: SearchRequest,
    principal: Principal = Depends(require_scope("read")),  # noqa: B008
) -> SearchResponse:
    """Hybrid search across a client's knowledge, grounded in source documents."""
    authorize_client(principal, client_id)
    settings = get_settings()
    masked = settings.mask_by_default if req.mask is None else req.mask
    request.state.audit_masked = masked
    top_k = min(req.top_k, settings.max_top_k)
    started = time.perf_counter()

    query_embedding = None
    if await pgvector_available():
        client = get_retrieval_client()
        try:
            vecs = await client.embed([req.query])
            query_embedding = vecs[0] if vecs else None
        finally:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()

    hits = await store.hybrid_search(
        client_id, query_text=req.query, query_embedding=query_embedding,
        scope_path=req.scope_path, doc_id=req.doc_id, top_k=top_k,
        current_only=req.current_only)
    observability.observe_search(time.perf_counter() - started)
    return SearchResponse(client_id=client_id, query=req.query, count=len(hits), masked=masked,
                          vector=query_embedding is not None,
                          hits=serving.project_nodes(hits, mask=masked))


@router.get("/search/health")
async def search_health() -> dict[str, str]:
    return {"status": "ok", "router": "search"}
