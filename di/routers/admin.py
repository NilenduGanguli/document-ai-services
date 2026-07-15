"""Admin router — data lifecycle + key management. Every route requires the ``admin`` scope.

Covers the capabilities an enterprise cannot adopt without: erasing a document, off-boarding a
tenant (right-to-erasure), adjudicating a disputed fact so the correction survives re-merge, and
issuing/revoking API keys.
"""
from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from di import auth, jobs, store
from di.auth import Principal, authorize_client, require_scope
from di.pipeline import _remerge_client_facts
from di.storage import get_blob_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class DeleteResult(BaseModel):
    client_id: str
    deleted: dict[str, int]
    remerged_facts: int | None = None


class PurgeRequest(BaseModel):
    """Tenant off-boarding is irreversible, so the caller must name the tenant explicitly."""

    confirm_client_id: str = Field(..., description="Must equal the client_id in the path")


class AdjudicationRequest(BaseModel):
    attribute_key: str
    verdict: str = Field(..., pattern="^(accept|reject|override)$")
    value_text: str | None = None
    value_date: date | None = None
    value_num: float | None = None
    reviewer: str | None = None
    note: str | None = None


class ApiKeyRequest(BaseModel):
    name: str
    client_ids: list[str] = Field(default_factory=lambda: ["*"])
    scopes: list[str] = Field(default_factory=lambda: ["read"])


class ApiKeyCreated(BaseModel):
    key_id: str
    api_key: str = Field(..., description="Shown once — it is stored only as a hash")
    name: str


@router.delete("/clients/{client_id}/documents/{doc_id}", response_model=DeleteResult)
async def delete_document(
    client_id: str, doc_id: str,
    principal: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> DeleteResult:
    """Hard-delete one document and everything derived from it, then re-merge the client view."""
    authorize_client(principal, client_id)
    doc = await store.get_document(client_id, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    blob_uri = doc.get("blob_uri")
    counts = await store.delete_document(client_id, doc_id)
    if blob_uri:
        try:
            await get_blob_store().delete(blob_uri, client_id=client_id)
        except Exception:  # noqa: BLE001 - row deletion already committed; log and continue
            logger.exception("blob delete failed for %s", blob_uri)
    # source_fact_ids has no FK, so the merged view must be recomputed from what remains.
    remerged = await _remerge_client_facts(client_id)
    return DeleteResult(client_id=client_id, deleted=counts, remerged_facts=remerged)


@router.post("/clients/{client_id}/purge", response_model=DeleteResult)
async def purge_client(
    client_id: str, body: PurgeRequest,
    principal: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> DeleteResult:
    """Erase every trace of a tenant: documents, versions, nodes, facts, jobs, blobs, audit."""
    authorize_client(principal, client_id)
    if body.confirm_client_id != client_id:
        raise HTTPException(status_code=400, detail="confirm_client_id does not match the path")
    counts = await store.purge_client(client_id)
    try:
        counts["blobs"] = await get_blob_store().delete_client(client_id)
    except Exception:  # noqa: BLE001
        logger.exception("blob purge failed for client %s", client_id)
    await jobs.purge_client_jobs(client_id)
    logger.warning("purged tenant %s by principal %s: %s", client_id, principal.name, counts)
    return DeleteResult(client_id=client_id, deleted=counts)


@router.post("/clients/{client_id}/adjudicate", response_model=dict)
async def adjudicate(
    client_id: str, body: AdjudicationRequest,
    principal: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> dict:
    """Record a reviewer's decision on a fact and re-merge so it takes effect immediately.

    The decision is stored separately from the derived facts, so the next ingest reapplies it
    instead of silently clobbering the correction.
    """
    authorize_client(principal, client_id)
    await store.upsert_adjudication(
        client_id, attribute_key=body.attribute_key, verdict=body.verdict,
        value_text=body.value_text, value_date=body.value_date, value_num=body.value_num,
        reviewer=body.reviewer or principal.name, note=body.note,
    )
    remerged = await _remerge_client_facts(client_id)
    return {"client_id": client_id, "attribute_key": body.attribute_key,
            "verdict": body.verdict, "remerged_facts": remerged}


@router.get("/keys", response_model=list[dict])
async def list_keys(
    _: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> list[dict]:
    """List issued API keys (metadata only — never hashes or key material)."""
    return await auth.list_api_keys()


@router.post("/keys", response_model=ApiKeyCreated, status_code=201)
async def create_key(
    body: ApiKeyRequest,
    _: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> ApiKeyCreated:
    """Issue an API key. The raw key is returned exactly once; only its hash is stored."""
    key_id, raw = await auth.create_api_key(
        name=body.name, client_ids=body.client_ids, scopes=body.scopes)
    return ApiKeyCreated(key_id=key_id, api_key=raw, name=body.name)


@router.delete("/keys/{key_id}", response_model=dict)
async def revoke_key(
    key_id: str,
    _: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> dict:
    """Revoke an API key immediately."""
    ok = await auth.revoke_api_key(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="key not found")
    return {"key_id": key_id, "revoked": True}
