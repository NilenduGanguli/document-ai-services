"""Clients router — per-client knowledge-tree traversal + the self-describing surfaces."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from di import serving, store

router = APIRouter(prefix="/api/v1/clients", tags=["clients"])


@router.get("/{client_id}/tree")
async def get_tree(client_id: str, doc_id: str | None = None, path: str | None = None,
                   max_depth: int | None = None, current_only: bool = True,
                   mask: bool = False) -> dict:
    rows = await store.fetch_subtree(client_id, doc_id=doc_id, path_prefix=path,
                                     max_depth=max_depth, current_only=current_only)
    return {"client_id": client_id, "count": len(rows), "tree": serving.nest_tree(rows, mask=mask)}


@router.get("/{client_id}/facts")
async def get_facts(client_id: str, attribute_key: str | None = None,
                    verified_only: bool = False, mask: bool = False) -> dict:
    rows = await store.fetch_merged_facts(client_id, attribute_key=attribute_key)
    facts = serving.project_facts(rows, mask=mask, verified_only=verified_only)
    return {"client_id": client_id, "count": len(facts), "facts": facts}


@router.get("/{client_id}/documents")
async def get_documents(client_id: str) -> dict:
    docs = await store.list_documents(client_id)
    return {"client_id": client_id, "count": len(docs), "documents": docs}


@router.get("/{client_id}/changes")
async def get_changes(client_id: str, since: str | None = None) -> dict:
    changes = await store.list_version_changes(client_id, since=since)
    return {"client_id": client_id, "count": len(changes), "changes": changes}


@router.get("/{client_id}/docs/{doc_id}/manifest")
async def get_manifest(client_id: str, doc_id: str) -> dict:
    doc = await store.get_document(client_id, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="document not found")
    nodes = await store.fetch_subtree(client_id, doc_id=doc_id)
    reps = await store.fetch_areps(client_id, doc_id=doc_id)
    return serving.build_manifest(doc, nodes, reps)


@router.get("/{client_id}/docs/{doc_id}/answerable")
async def get_answerable(client_id: str, doc_id: str) -> dict:
    reps = await store.fetch_areps(client_id, doc_id=doc_id)
    return {"client_id": client_id, "doc_id": doc_id,
            "answerable": serving.answerable_questions(reps)}


@router.get("/health")
async def clients_health() -> dict[str, str]:
    return {"status": "ok", "router": "clients"}
