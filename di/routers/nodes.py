"""Nodes router — node-level provenance lookup.

``client_id`` is required (RLS scope): every answer is one hop from its exact source document /
page / bounding box / extractor / confidence.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from di import store
from di.auth import Principal, authorize_client, require_principal

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])


class ProvenanceResponse(BaseModel):
    node_id: str
    client_id: str
    doc_id: str | None = None
    version_id: str | None = None
    node_type: str | None = None
    attribute_key: str | None = None
    verification_status: str | None = None
    confidence: float | None = None
    provenance: dict[str, Any] | None = None


@router.get("/{node_id}/provenance", response_model=ProvenanceResponse)
async def get_provenance(
    node_id: str, client_id: str,
    principal: Principal = Depends(require_principal),  # noqa: B008
) -> ProvenanceResponse:
    """Trace one node back to its exact source: document, page, bbox, extractor, model."""
    authorize_client(principal, client_id)
    node = await store.fetch_node(client_id, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return ProvenanceResponse(
        node_id=node_id,
        client_id=client_id,
        doc_id=str(node.get("doc_id")) if node.get("doc_id") else None,
        version_id=str(node.get("version_id")) if node.get("version_id") else None,
        node_type=node.get("node_type"),
        attribute_key=node.get("attribute_key"),
        verification_status=node.get("verification_status"),
        confidence=node.get("confidence"),
        provenance=node.get("provenance"),
    )


@router.get("/health")
async def nodes_health() -> dict[str, str]:
    return {"status": "ok", "router": "nodes"}
