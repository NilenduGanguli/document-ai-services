"""Search router — hybrid (dense + lexical + structural) retrieval scoped to a client.

Embeds the query through the retrieval gateway (when pgvector is available), runs the
index-many/return-parent hybrid search, and returns ranked knode hits with grounding. Honors the
toggleable masking projection.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from di import serving, store
from di.db import pgvector_available
from di.retrieval_client import get_retrieval_client

router = APIRouter(prefix="/api/v1", tags=["search"])


class SearchRequest(BaseModel):
    query: str
    scope_path: str | None = None
    doc_id: str | None = None
    top_k: int = 20
    current_only: bool = True
    mask: bool = False


@router.post("/clients/{client_id}/search")
async def search(client_id: str, req: SearchRequest) -> dict:
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
        scope_path=req.scope_path, doc_id=req.doc_id, top_k=req.top_k,
        current_only=req.current_only)
    return {"client_id": client_id, "query": req.query, "count": len(hits),
            "hits": serving.project_nodes(hits, mask=req.mask)}


@router.get("/search/health")
async def search_health() -> dict[str, str]:
    return {"status": "ok", "router": "search"}
