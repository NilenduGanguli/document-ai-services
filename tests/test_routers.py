"""Router wiring tests — call the endpoint coroutines directly with monkeypatched store.

No DB, no app startup, no network. Validates routing, serving integration, authorization and the
mask flag. The endpoints now take an injected ``Principal``; calling them directly means passing
one explicitly (FastAPI would otherwise resolve it from the X-API-KEY header).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from di import store
from di.auth import Principal
from di.routers import clients, nodes, search


def _principal(client_ids: list[str] | None = None) -> Principal:
    return Principal(key_id="test-key", name="test", client_ids=client_ids or ["*"],
                     scopes=["*"])


def _request() -> Request:
    """A minimal ASGI request — just enough for handlers that stash request.state.audit_masked."""
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


def _knode(nid, parent, ntype, **kw):
    base = {
        "id": nid, "parent_id": parent, "node_type": ntype, "seq": 0, "depth": 1, "path": "a",
        "title": None, "content": None, "context_prefix": None, "attribute_key": None,
        "value_text": None, "value_date": None, "value_num": None,
        "verification_status": "unverified", "confidence": 0.5, "sensitivity": "LOW",
        "valid_from": None, "valid_to": None, "provenance": {}, "doc_id": "d1", "version_id": "v1",
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_get_tree_nests_and_masks(monkeypatch):
    rows = [
        _knode("root", None, "document", path="a"),
        _knode("f1", "root", "fact", path="a.f1", value_text="X1234567",
               sensitivity="CRITICAL", attribute_key="id.passport_number"),
    ]

    async def fake_fetch_subtree(client_id, **kw):
        return rows

    monkeypatch.setattr(store, "fetch_subtree", fake_fetch_subtree)
    res = await clients.get_tree(_request(), "c1", mask=True, principal=_principal())
    assert res.count == 2
    assert res.masked is True
    root = res.tree[0]
    fact = root["children"][0]
    assert fact["masked"] is True and fact["value_text"].endswith("4567")


@pytest.mark.asyncio
async def test_get_tree_denies_other_tenants(monkeypatch):
    """A principal scoped to one client cannot read another's tree."""
    with pytest.raises(HTTPException) as ei:
        await clients.get_tree(_request(), "other-client",
                               principal=_principal(client_ids=["c1"]))
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_get_facts_verified_only(monkeypatch):
    async def fake(client_id, *, attribute_key=None):
        return [
            {"attribute_key": "id.ssn", "resolved_value": "536-90-4399", "confidence": 0.95,
             "conflict": False, "verification_status": "checksum_verified"},
            {"attribute_key": "income.employer", "resolved_value": "Acme", "confidence": 0.4,
             "conflict": True, "verification_status": "llm_unverified"},
        ]

    monkeypatch.setattr(store, "fetch_merged_facts", fake)
    res = await clients.get_facts(_request(), "c1", verified_only=True, mask=False,
                                  principal=_principal())
    assert res.count == 1 and res.facts[0]["attribute_key"] == "id.ssn"


@pytest.mark.asyncio
async def test_get_facts_excludes_high_confidence_llm_fact(monkeypatch):
    """A model's self-reported 0.95 must NOT satisfy verified_only — only real verification does."""
    async def fake(client_id, *, attribute_key=None):
        return [
            {"attribute_key": "income.annual", "resolved_value": "250000", "confidence": 0.95,
             "conflict": False, "verification_status": "llm_unverified"},
        ]

    monkeypatch.setattr(store, "fetch_merged_facts", fake)
    res = await clients.get_facts(_request(), "c1", verified_only=True, mask=False,
                                  principal=_principal())
    assert res.count == 0


@pytest.mark.asyncio
async def test_manifest(monkeypatch):
    async def fake_doc(client_id, doc_id):
        return {"id": "d1", "document_name": "ine.pdf", "doc_type": "MX_INE", "jurisdiction": "MX",
                "page_count": 1, "lang_profile": {"dominant_lang": "es"},
                "sensitivity_bucket": "CRITICAL", "gate_decision": "DETERMINISTIC_ONLY"}

    async def fake_nodes(client_id, **kw):
        return [_knode("f1", None, "fact", attribute_key="id.curp")]

    async def fake_reps(client_id, **kw):
        return [{"rep_type": "hypothetical_q", "rep_text": "Q?", "knode_id": "f1", "path": "a",
                 "rep_lang": "es"}]

    monkeypatch.setattr(store, "get_document", fake_doc)
    monkeypatch.setattr(store, "fetch_subtree", fake_nodes)
    monkeypatch.setattr(store, "fetch_areps", fake_reps)
    res = await clients.get_manifest("c1", "d1", principal=_principal())
    assert res["doc_type"] == "MX_INE" and res["answerable"] is True
    assert "id.curp" in res["attribute_keys"]


@pytest.mark.asyncio
async def test_get_documents_surfaces_the_blob_location(monkeypatch):
    """An authorized caller can see WHERE its document's raw bytes were retained.

    The blob URI is tenant-scoped and every backend re-checks tenant ownership of a presented URI
    before reading it (tests/test_storage.py), so returning it grants no access — it makes the
    "the upload is stored, and here is where" story answerable through the API instead of only
    through SQL.
    """
    row = {
        "id": "00000000-0000-0000-0000-0000000000d1",
        "client_id": "c1", "document_name": "passport.pdf", "external_document_id": None,
        "doc_type": "PASSPORT", "sha256": "ab" * 32, "mime": "application/pdf",
        "blob_uri": "s3://document-intelligence/documents/c1/" + "ab" * 32 + "/passport.pdf",
        "blob_backend": "s3",
    }

    async def fake_list_documents(client_id, **kw):
        return [row], None

    monkeypatch.setattr(store, "list_documents", fake_list_documents)
    # Called directly, so the Query()/None defaults FastAPI would resolve are passed explicitly.
    res = await clients.get_documents("c1", limit=None, cursor=None, principal=_principal())

    assert res.count == 1
    doc = res.documents[0]
    assert doc.blob_backend == "s3"
    assert doc.blob_uri == row["blob_uri"]


def test_document_list_columns_include_the_blob_location():
    """The projection is an explicit column list, so a field on the response model that is not
    selected would silently be None forever. Raw OCR stays excluded (payload + PII)."""
    assert "blob_uri" in store._DOC_LIST_COLS  # noqa: SLF001 - the projection IS the unit here
    assert "blob_backend" in store._DOC_LIST_COLS  # noqa: SLF001
    assert "ocr_text" not in store._DOC_LIST_COLS  # noqa: SLF001
    assert "ocr_lines" not in store._DOC_LIST_COLS  # noqa: SLF001


@pytest.mark.asyncio
async def test_search_returns_ranked_hits(monkeypatch):
    async def fake_pgvector():
        return False  # skip embedding path

    async def fake_hybrid(client_id, **kw):
        return [_knode("c1", None, "chunk", content="passport number", _rank=1, _score=0.9)]

    monkeypatch.setattr(search, "pgvector_available", fake_pgvector)
    monkeypatch.setattr(store, "hybrid_search", fake_hybrid)
    req = search.SearchRequest(query="passport", top_k=5, mask=False)
    res = await search.search(_request(), "c1", req, principal=_principal())
    assert res.count == 1 and res.hits[0]["_rank"] == 1
    assert res.vector is False


@pytest.mark.asyncio
async def test_provenance(monkeypatch):
    async def fake_node(client_id, node_id):
        return _knode(node_id, None, "fact", attribute_key="id.curp",
                      verification_status="checksum_verified", provenance={"page": 1})

    monkeypatch.setattr(store, "fetch_node", fake_node)
    res = await nodes.get_provenance("n1", client_id="c1", principal=_principal())
    assert res.verification_status == "checksum_verified"
    assert res.provenance == {"page": 1}


@pytest.mark.asyncio
async def test_provenance_404(monkeypatch):
    async def fake_node(client_id, node_id):
        return None

    monkeypatch.setattr(store, "fetch_node", fake_node)
    with pytest.raises(HTTPException) as ei:
        await nodes.get_provenance("missing", client_id="c1", principal=_principal())
    assert ei.value.status_code == 404
