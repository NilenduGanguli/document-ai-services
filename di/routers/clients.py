"""Clients router — per-client knowledge-tree traversal + the self-describing surfaces.

Every route is authenticated and authorized to the path's ``client_id``. Responses are typed so
the OpenAPI schema generates real SDK models instead of ``Dict[str, Any]``. Masking defaults to
the server-side policy (``settings.mask_by_default``) rather than to "off", so a caller who omits
the parameter cannot accidentally pull unredacted national IDs into their logs.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from di import serving, store
from di.auth import Principal, authorize_client, require_principal
from di.config import get_settings
from di.store import clamp_limit

router = APIRouter(prefix="/api/v1/clients", tags=["clients"])


def _mask_default(mask: bool | None) -> bool:
    return get_settings().mask_by_default if mask is None else mask


# --------------------------------------------------------------------------- responses
class TreeResponse(BaseModel):
    client_id: str
    count: int
    masked: bool
    tree: list[dict[str, Any]] = Field(default_factory=list)


class FactsResponse(BaseModel):
    client_id: str
    count: int
    masked: bool
    facts: list[dict[str, Any]] = Field(default_factory=list)


class DocumentSummary(BaseModel):
    id: str
    document_name: str
    external_document_id: str | None = None
    doc_type: str | None = None
    doc_category: str | None = None
    jurisdiction: str | None = None
    sensitivity_bucket: str | None = None
    gate_decision: str | None = None
    confidence: float | None = None
    ocr_engine: str | None = None
    page_count: int | None = None
    mime: str | None = None
    sha256: str | None = None
    blob_backend: str | None = None
    created_at: Any = None
    updated_at: Any = None


class DocumentsResponse(BaseModel):
    client_id: str
    count: int
    documents: list[DocumentSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class ChangesResponse(BaseModel):
    client_id: str
    count: int
    changes: list[dict[str, Any]] = Field(default_factory=list)
    next_seq: int | None = Field(None, description="Pass back as after_seq to resume exactly")


class AnswerableResponse(BaseModel):
    client_id: str
    doc_id: str
    answerable: list[dict[str, Any]] = Field(default_factory=list)


# --------------------------------------------------------------------------- routes
@router.get("/{client_id}/tree", response_model=TreeResponse)
async def get_tree(
    client_id: str, doc_id: str | None = None, path: str | None = None,
    max_depth: int | None = Query(None, ge=1, le=32),  # noqa: B008
    current_only: bool = True, mask: bool | None = None,
    principal: Principal = Depends(require_principal),  # noqa: B008
) -> TreeResponse:
    """Nested knowledge subtree for a client (optionally scoped to a document or path)."""
    authorize_client(principal, client_id)
    masked = _mask_default(mask)
    rows = await store.fetch_subtree(client_id, doc_id=doc_id, path_prefix=path,
                                     max_depth=max_depth, current_only=current_only)
    return TreeResponse(client_id=client_id, count=len(rows), masked=masked,
                        tree=serving.nest_tree(rows, mask=masked))


@router.get("/{client_id}/facts", response_model=FactsResponse)
async def get_facts(
    client_id: str, attribute_key: str | None = None, verified_only: bool = False,
    mask: bool | None = None,
    principal: Principal = Depends(require_principal),  # noqa: B008
) -> FactsResponse:
    """Merged client-level facts. ``verified_only`` means independently verified, not self-scored."""
    authorize_client(principal, client_id)
    masked = _mask_default(mask)
    rows = await store.fetch_merged_facts(client_id, attribute_key=attribute_key)
    facts = serving.project_facts(rows, mask=masked, verified_only=verified_only)
    return FactsResponse(client_id=client_id, count=len(facts), masked=masked, facts=facts)


@router.get("/{client_id}/documents", response_model=DocumentsResponse)
async def get_documents(
    client_id: str,
    limit: int | None = Query(None, ge=1, le=200),  # noqa: B008
    cursor: str | None = None,
    principal: Principal = Depends(require_principal),  # noqa: B008
) -> DocumentsResponse:
    """List a client's documents (keyset-paginated; excludes raw OCR text by design)."""
    authorize_client(principal, client_id)
    try:
        docs, next_cursor = await store.list_documents(client_id, limit=clamp_limit(limit),
                                                       cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return DocumentsResponse(
        client_id=client_id, count=len(docs),
        documents=[DocumentSummary(**{**d, "id": str(d["id"])}) for d in docs],
        next_cursor=next_cursor,
    )


@router.get("/{client_id}/changes", response_model=ChangesResponse)
async def get_changes(
    client_id: str, since: str | None = None,
    after_seq: int | None = Query(None, description="Monotonic cursor; preferred over `since`"),  # noqa: B008
    limit: int | None = Query(None, ge=1, le=200),  # noqa: B008
    principal: Principal = Depends(require_principal),  # noqa: B008
) -> ChangesResponse:
    """Version change feed. Resume with ``after_seq`` for exactly-once-ish delivery."""
    authorize_client(principal, client_id)
    changes, next_seq = await store.list_version_changes(
        client_id, since=since, after_seq=after_seq, limit=clamp_limit(limit))
    return ChangesResponse(client_id=client_id, count=len(changes), changes=changes,
                           next_seq=next_seq)


@router.get("/{client_id}/docs/{doc_id}/manifest")
async def get_manifest(
    client_id: str, doc_id: str,
    principal: Principal = Depends(require_principal),  # noqa: B008
) -> dict[str, Any]:
    """Per-document capabilities manifest: what this document can answer and how."""
    authorize_client(principal, client_id)
    doc = await store.get_document(client_id, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    nodes = await store.fetch_subtree(client_id, doc_id=doc_id)
    reps = await store.fetch_areps(client_id, doc_id=doc_id)
    return serving.build_manifest(doc, nodes, reps)


@router.get("/{client_id}/docs/{doc_id}/answerable", response_model=AnswerableResponse)
async def get_answerable(
    client_id: str, doc_id: str,
    principal: Principal = Depends(require_principal),  # noqa: B008
) -> AnswerableResponse:
    """The questions this document can answer, derived from its accessibility representations."""
    authorize_client(principal, client_id)
    reps = await store.fetch_areps(client_id, doc_id=doc_id)
    return AnswerableResponse(client_id=client_id, doc_id=doc_id,
                              answerable=serving.answerable_questions(reps))


@router.get("/health")
async def clients_health() -> dict[str, str]:
    return {"status": "ok", "router": "clients"}
