"""Router wiring tests — call the endpoint coroutines directly with monkeypatched store.

No DB, no app startup, no network. Validates routing, serving integration, and the mask flag.
"""
from __future__ import annotations

import pytest

from di import store
from di.routers import clients, nodes, search


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
    res = await clients.get_tree("c1", mask=True)
    assert res["count"] == 2
    root = res["tree"][0]
    fact = root["children"][0]
    assert fact["masked"] is True and fact["value_text"].endswith("4567")


@pytest.mark.asyncio
async def test_get_facts_verified_only(monkeypatch):
    async def fake(client_id, *, attribute_key=None):
        return [
            {"attribute_key": "id.ssn", "resolved_value": "536-90-4399", "confidence": 0.95,
             "conflict": False},
            {"attribute_key": "income.employer", "resolved_value": "Acme", "confidence": 0.4,
             "conflict": True},
        ]

    monkeypatch.setattr(store, "fetch_merged_facts", fake)
    res = await clients.get_facts("c1", verified_only=True)
    assert res["count"] == 1 and res["facts"][0]["attribute_key"] == "id.ssn"


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
    res = await clients.get_manifest("c1", "d1")
    assert res["doc_type"] == "MX_INE" and res["answerable"] is True
    assert "id.curp" in res["attribute_keys"]


@pytest.mark.asyncio
async def test_search_returns_ranked_hits(monkeypatch):
    async def fake_pgvector():
        return False  # skip embedding path

    async def fake_hybrid(client_id, **kw):
        return [_knode("c1", None, "chunk", content="passport number", _rank=1, _score=0.9)]

    monkeypatch.setattr(search, "pgvector_available", fake_pgvector)
    monkeypatch.setattr(store, "hybrid_search", fake_hybrid)
    req = search.SearchRequest(query="passport", top_k=5)
    res = await search.search("c1", req)
    assert res["count"] == 1 and res["hits"][0]["_rank"] == 1


@pytest.mark.asyncio
async def test_provenance(monkeypatch):
    async def fake_node(client_id, node_id):
        return _knode(node_id, None, "fact", attribute_key="id.curp",
                      verification_status="checksum_verified", provenance={"page": 1})

    monkeypatch.setattr(store, "fetch_node", fake_node)
    res = await nodes.get_provenance("n1", client_id="c1")
    assert res["verification_status"] == "checksum_verified"
    assert res["provenance"] == {"page": 1}


@pytest.mark.asyncio
async def test_provenance_404(monkeypatch):
    from fastapi import HTTPException

    async def fake_node(client_id, node_id):
        return None

    monkeypatch.setattr(store, "fetch_node", fake_node)
    with pytest.raises(HTTPException) as ei:
        await nodes.get_provenance("missing", client_id="c1")
    assert ei.value.status_code == 404
