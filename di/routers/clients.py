"""Clients router — per-client tree / facts / documents / changes. Placeholder; M2–M5.

Owns endpoints:
  GET /api/v1/clients/{id}/tree          (sub)tree as nested JSON (?doc_type,&version,&path,&depth,&mask)
  GET /api/v1/clients/{id}/facts         merged + per-doc facts (?attribute_key,&verified_only,&mask)
  GET /api/v1/clients/{id}/documents     doc inventory + versions
  GET /api/v1/clients/{id}/changes       version delta feed (?since)
  GET /api/v1/clients/{id}/docs/{doc}/manifest      capabilities manifest
  GET /api/v1/clients/{id}/docs/{doc}/answerable     answerable-questions index
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/clients", tags=["clients"])


@router.get("/health")
async def clients_health() -> dict[str, str]:
    return {"status": "ok", "router": "clients"}
