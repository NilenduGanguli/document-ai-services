"""Nodes router — node-level provenance lookup.

``client_id`` is required (RLS scope): every answer is one hop from its exact source document /
page / bounding box / extractor / confidence.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from di import store

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])


@router.get("/{node_id}/provenance")
async def get_provenance(node_id: str, client_id: str) -> dict:
    node = await store.fetch_node(client_id, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return {
        "node_id": node_id,
        "client_id": client_id,
        "doc_id": str(node.get("doc_id")),
        "version_id": str(node.get("version_id")),
        "node_type": node.get("node_type"),
        "attribute_key": node.get("attribute_key"),
        "verification_status": node.get("verification_status"),
        "confidence": node.get("confidence"),
        "provenance": node.get("provenance"),
    }


@router.get("/health")
async def nodes_health() -> dict[str, str]:
    return {"status": "ok", "router": "nodes"}
