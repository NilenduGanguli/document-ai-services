"""Admin router — data lifecycle + key management. Every route requires the ``admin`` scope.

Covers the capabilities an enterprise cannot adopt without: erasing a document, off-boarding a
tenant (right-to-erasure), adjudicating a disputed fact so the correction survives re-merge, and
issuing/revoking API keys.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from di import auth, jobs, ontology, store
from di.auth import Principal, authorize_client, require_scope
from di.config import get_settings
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
    #: Required for multi-valued attributes (see di.ontology.MULTI_VALUED_ATTRIBUTE_KEYS); must be
    #: '' for single-valued ones. Identifies WHICH instance (director, account...) this verdict
    #: applies to.
    instance_key: str = ""
    verdict: str = Field(..., pattern="^(accept|reject|override)$")
    value_text: str | None = None
    value_date: date | None = None
    value_num: float | None = None
    reviewer: str | None = None
    note: str | None = None


class ClearAdjudicationResponse(BaseModel):
    client_id: str
    attribute_key: str
    instance_key: str
    cleared: bool
    remerged_facts: int | None = None


class ApiKeyRequest(BaseModel):
    name: str
    client_ids: list[str] = Field(default_factory=lambda: ["*"])
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    expires_at: datetime | None = Field(
        None, description="When the key stops authenticating. Recommended for anything beyond a "
                          "local demo — unset means the key never expires."
    )
    rate_limit_rps: float | None = Field(
        None, description="Per-key rate-limit override; unset uses the fleet default."
    )


class ApiKeyCreated(BaseModel):
    key_id: str
    api_key: str = Field(..., description="Shown once — it is stored only as a hash")
    name: str
    expires_at: datetime | None = None


class RotateKeyRequest(BaseModel):
    overlap_hours: int | None = Field(
        None, description="How long the old key stays valid; defaults to "
                          "settings.key_rotation_overlap_hours, capped at 168 (1 week)."
    )


class RotateKeyResponse(BaseModel):
    key_id: str
    api_key: str = Field(..., description="The NEW key's raw secret — shown once")
    name: str
    old_key_expires_at: datetime


class TenantPolicyRequest(BaseModel):
    max_active_jobs: int | None = Field(
        None, description="NULL = use the fleet default (settings.ingest_max_active_jobs_per_client)"
    )
    daily_ingest_limit: int | None = Field(
        None, description="NULL = use the fleet default; 0 = blocked entirely"
    )
    note: str | None = None


class TenantPolicyResponse(BaseModel):
    client_id: str
    max_active_jobs: int | None
    daily_ingest_limit: int | None
    note: str | None
    updated_at: datetime


class AccessLogResponse(BaseModel):
    count: int
    entries: list[dict] = Field(default_factory=list)
    next_cursor: str | None = None


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


async def _instance_exists(client_id: str, attribute_key: str, instance_key: str) -> bool:
    """True if ``instance_key`` references either a live merged row OR an existing adjudication
    row for this (client_id, attribute_key).

    The adjudication check is what makes reject reversible: a reject on a multi-valued instance
    deletes its client_merged_fact row (di.subtree.merge omits it from the output entirely), so
    checking only the merged rows would make every subsequent verdict on that instance 404
    forever — the reviewer who rejected the wrong director could never accept/override/re-review
    it. See also DELETE .../adjudications/{attribute_key} for undoing a verdict outright.
    """
    merged = await store.fetch_merged_facts(client_id, attribute_key=attribute_key)
    if any(r.get("instance_key") == instance_key for r in merged):
        return True
    adjudications = await store.fetch_adjudications(client_id)
    return any(r.get("attribute_key") == attribute_key and r.get("instance_key") == instance_key
              for r in adjudications)


@router.post("/clients/{client_id}/adjudicate", response_model=dict)
async def adjudicate(
    client_id: str, body: AdjudicationRequest,
    principal: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> dict:
    """Record a reviewer's decision on a fact (or one instance of a multi-valued attribute) and
    re-merge so it takes effect immediately.

    The decision is stored separately from the derived facts, so the next ingest reapplies it
    instead of silently clobbering the correction. Fail-closed on cardinality: a multi-valued
    attribute requires a non-empty ``instance_key`` that resolves to a real instance (422/404
    otherwise — prevents a typo'd fingerprint from silently doing nothing); a single-valued one
    rejects a non-empty ``instance_key`` outright.
    """
    authorize_client(principal, client_id)
    cardinality = ontology.cardinality_for(body.attribute_key)
    if cardinality == "multi":
        if not body.instance_key:
            raise HTTPException(
                status_code=422,
                detail=f"{body.attribute_key} is multi-valued — instance_key is required")
        if not await _instance_exists(client_id, body.attribute_key, body.instance_key):
            raise HTTPException(
                status_code=404,
                detail=f"no merged or previously-adjudicated instance "
                       f"{body.attribute_key}:{body.instance_key} for {client_id}")
    elif body.instance_key:
        raise HTTPException(
            status_code=422,
            detail=f"{body.attribute_key} is single-valued — instance_key must be empty")
    await store.upsert_adjudication(
        client_id, attribute_key=body.attribute_key, instance_key=body.instance_key,
        verdict=body.verdict, value_text=body.value_text, value_date=body.value_date,
        value_num=body.value_num, reviewer=body.reviewer or principal.name, note=body.note,
    )
    remerged = await _remerge_client_facts(client_id)
    return {"client_id": client_id, "attribute_key": body.attribute_key,
            "instance_key": body.instance_key, "verdict": body.verdict,
            "remerged_facts": remerged}


@router.get("/clients/{client_id}/adjudications", response_model=list[dict])
async def list_adjudications(
    client_id: str,
    principal: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> list[dict]:
    """Live verdicts, one per (attribute_key, instance_key) — including multi-valued-instance
    rejects, whose merged row is otherwise invisible (removed from client_merged_fact on reject)."""
    authorize_client(principal, client_id)
    return await store.fetch_adjudications(client_id)


@router.get("/clients/{client_id}/adjudications/history", response_model=list[dict])
async def adjudication_history(
    client_id: str, attribute_key: str | None = None, instance_key: str | None = None,
    principal: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> list[dict]:
    """The durable, append-only compliance record — every verdict ever recorded, including ones
    later overwritten or cleared. ``GET .../adjudications`` above only shows the current live
    verdict; a bank's audit needs "what did we decide, and when, and did it change" too.
    """
    authorize_client(principal, client_id)
    return await store.fetch_adjudication_events(
        client_id, attribute_key=attribute_key, instance_key=instance_key)


@router.delete("/clients/{client_id}/adjudications/{attribute_key}",
               response_model=ClearAdjudicationResponse)
async def clear_adjudication(
    client_id: str, attribute_key: str, instance_key: str = "", reviewer: str | None = None,
    principal: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> ClearAdjudicationResponse:
    """Remove a live verdict so the next re-merge falls back to automatic resolution.

    ``instance_key`` is a query parameter (default ``''``) rather than a second path segment: an
    empty path segment does not route cleanly, and single-valued attributes need to reach this
    endpoint too (even though their reject is not a one-way door — this exists primarily to undo a
    wrong reject/override on a multi-valued instance).
    """
    authorize_client(principal, client_id)
    cleared = await store.clear_adjudication(
        client_id, attribute_key=attribute_key, instance_key=instance_key,
        reviewer=reviewer or principal.name)
    remerged = await _remerge_client_facts(client_id) if cleared else None
    return ClearAdjudicationResponse(client_id=client_id, attribute_key=attribute_key,
                                     instance_key=instance_key, cleared=cleared,
                                     remerged_facts=remerged)


@router.get("/keys", response_model=list[dict])
async def list_keys(
    _: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> list[dict]:
    """List issued API keys (metadata only — never hashes or key material)."""
    return await auth.list_api_keys()


@router.post("/keys", response_model=ApiKeyCreated, status_code=201)
async def create_key(
    body: ApiKeyRequest,
    principal: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> ApiKeyCreated:
    """Issue an API key. The raw key is returned exactly once; only its hash is stored."""
    key_id, raw = await auth.create_api_key(
        name=body.name, client_ids=body.client_ids, scopes=body.scopes,
        expires_at=body.expires_at, created_by=principal.name, rate_limit_rps=body.rate_limit_rps,
    )
    return ApiKeyCreated(key_id=key_id, api_key=raw, name=body.name, expires_at=body.expires_at)


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


@router.post("/keys/{key_id}/rotate", response_model=RotateKeyResponse)
async def rotate_key(
    key_id: str, body: RotateKeyRequest,
    _: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> RotateKeyResponse:
    """Rotate a key: mint a successor with identical grants, time-box the predecessor.

    Flow: create-new -> overlap-window -> old key auto-expires. Use ``DELETE /keys/{id}`` instead
    if the old key must die immediately rather than at the end of the overlap window.
    """
    new_key_id, raw, old_expires_at = await auth.rotate_api_key(
        key_id, overlap_hours=body.overlap_hours)
    keys = await auth.list_api_keys()
    name = next((k["name"] for k in keys if k["id"] == new_key_id), "")
    return RotateKeyResponse(key_id=new_key_id, api_key=raw, name=name,
                             old_key_expires_at=old_expires_at)


@router.get("/tenants/{client_id}/policy", response_model=TenantPolicyResponse)
async def get_tenant_policy(
    client_id: str,
    principal: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> TenantPolicyResponse:
    """Fetch a tenant's ingest-quota overrides, or the fleet defaults if none are set."""
    authorize_client(principal, client_id)
    settings = get_settings()
    policy = await store.fetch_tenant_policy(client_id)
    if policy is None:
        return TenantPolicyResponse(
            client_id=client_id, max_active_jobs=settings.ingest_max_active_jobs_per_client,
            daily_ingest_limit=settings.ingest_daily_limit_per_client, note="(fleet defaults)",
            updated_at=datetime.now(),
        )
    return TenantPolicyResponse(**policy)


@router.put("/tenants/{client_id}/policy", response_model=TenantPolicyResponse)
async def put_tenant_policy(
    client_id: str, body: TenantPolicyRequest,
    principal: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> TenantPolicyResponse:
    """Set a tenant's ingest-quota overrides. Takes effect on the next admission check."""
    authorize_client(principal, client_id)
    policy = await store.upsert_tenant_policy(
        client_id, max_active_jobs=body.max_active_jobs,
        daily_ingest_limit=body.daily_ingest_limit, note=body.note,
    )
    logger.info("tenant policy updated for %s by %s: %s", client_id, principal.name, policy)
    return TenantPolicyResponse(**policy)


@router.get("/access-log", response_model=AccessLogResponse)
async def get_access_log(
    client_id: str | None = None,
    limit: int | None = Query(None, ge=1, le=200),  # noqa: B008
    cursor: str | None = None,
    principal: Principal = Depends(require_scope("admin")),  # noqa: B008
) -> AccessLogResponse:
    """Query the read-side access audit — answers "who accessed this client's data?".

    A ``client_id``-filtered query requires the caller be authorized for that tenant; an
    unfiltered (fleet-wide) query requires the wildcard tenant grant, since it would otherwise
    let an admin key scoped to one tenant read every other tenant's access history.
    """
    if client_id:
        authorize_client(principal, client_id)
    else:
        # authorize_client("*") requires the WILDCARD grant specifically: can_access("*") is only
        # true when "*" appears in principal.client_ids, since a literal tenant id can never be
        # the string "*". A principal scoped to ["acme"] gets 403 here, exactly as intended.
        authorize_client(principal, "*")
    entries, next_cursor = await store.fetch_access_log(
        client_id=client_id, limit=store.clamp_limit(limit), cursor=cursor)
    return AccessLogResponse(count=len(entries), entries=entries, next_cursor=next_cursor)
