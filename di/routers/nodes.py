"""Nodes router — `GET /api/v1/nodes/{id}/provenance`. Placeholder; M2.

Owns node-level provenance lookup (source document / page / bbox / extractor / confidence).
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/nodes", tags=["nodes"])


@router.get("/health")
async def nodes_health() -> dict[str, str]:
    return {"status": "ok", "router": "nodes"}
