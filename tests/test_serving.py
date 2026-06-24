"""Unit tests for the serving layer (di.serving) — pure, no DB/network."""
from __future__ import annotations

from di import serving


def _node(nid, parent, ntype, *, seq=0, path="p", value_text=None, content=None,
          sensitivity="LOW", attribute_key=None):
    return {
        "id": nid, "parent_id": parent, "node_type": ntype, "seq": seq, "depth": 1, "path": path,
        "title": None, "content": content, "context_prefix": None, "attribute_key": attribute_key,
        "value_text": value_text, "value_date": None, "value_num": None,
        "verification_status": "unverified", "confidence": 0.5, "sensitivity": sensitivity,
        "valid_from": None, "valid_to": None, "provenance": {}, "doc_id": "d1", "version_id": "v1",
    }


def test_nest_tree_parent_child_and_order():
    rows = [
        _node("root", None, "document", seq=0, path="a"),
        _node("s1", "root", "section", seq=1, path="a.s1"),
        _node("s0", "root", "section", seq=0, path="a.s0"),
        _node("c0", "s0", "chunk", seq=0, path="a.s0.c0", content="hello"),
    ]
    tree = serving.nest_tree(rows)
    assert len(tree) == 1
    root = tree[0]
    assert root["id"] == "root"
    # children ordered by seq -> s0 before s1
    assert [c["id"] for c in root["children"]] == ["s0", "s1"]
    assert root["children"][0]["children"][0]["id"] == "c0"


def test_masking_redacts_only_sensitive_payload():
    rows = [
        _node("f1", None, "fact", value_text="X1234567", sensitivity="CRITICAL",
              attribute_key="id.passport_number"),
        _node("c1", None, "chunk", content="public heading text", sensitivity="LOW"),
    ]
    masked = serving.nest_tree(rows, mask=True)
    fact = next(n for n in masked if n["id"] == "f1")
    chunk = next(n for n in masked if n["id"] == "c1")
    assert fact["value_text"].endswith("4567") and "•" in fact["value_text"]
    assert fact["masked"] is True
    # non-sensitive content untouched; structure intact
    assert chunk["content"] == "public heading text"
    assert "masked" not in chunk


def test_masking_off_leaves_everything():
    rows = [_node("f1", None, "fact", value_text="X1234567", sensitivity="CRITICAL")]
    out = serving.nest_tree(rows, mask=False)
    assert out[0]["value_text"] == "X1234567"


def test_build_manifest_and_answerable():
    knodes = [
        _node("root", None, "document", path="a"),
        _node("f1", "root", "fact", attribute_key="id.curp", path="a.f1"),
        _node("f2", "root", "fact", attribute_key="identity.date_of_birth", path="a.f2"),
        _node("c1", "root", "chunk", path="a.c1"),
    ]
    areps = [
        {"rep_type": "hypothetical_q", "rep_text": "What is the CURP?", "knode_id": "f1",
         "path": "a.f1", "rep_lang": "es"},
        {"rep_type": "summary", "rep_text": "...", "knode_id": "c1", "path": "a.c1", "rep_lang": "en"},
    ]
    doc = {"id": "d1", "document_name": "ine.pdf", "doc_type": "MX_INE", "jurisdiction": "MX",
           "page_count": 1, "lang_profile": {"dominant_lang": "es"}, "sensitivity_bucket": "CRITICAL",
           "gate_decision": "DETERMINISTIC_ONLY"}
    manifest = serving.build_manifest(doc, knodes, areps)
    assert manifest["node_type_counts"]["fact"] == 2
    assert set(manifest["attribute_keys"]) == {"id.curp", "identity.date_of_birth"}
    assert manifest["answerable"] is True
    qs = serving.answerable_questions(areps)
    assert len(qs) == 1 and qs[0]["question"] == "What is the CURP?"


def test_project_facts_verified_and_masking():
    rows = [
        {"attribute_key": "id.ssn", "resolved_value": "536-90-4399", "confidence": 0.95,
         "conflict": False},
        {"attribute_key": "income.employer", "resolved_value": "Acme", "confidence": 0.5,
         "conflict": True},
    ]
    all_facts = serving.project_facts(rows)
    assert all_facts[0]["verified"] is True and all_facts[0]["sensitivity"] == "CRITICAL"
    assert all_facts[1]["verified"] is False
    verified = serving.project_facts(rows, verified_only=True)
    assert len(verified) == 1 and verified[0]["attribute_key"] == "id.ssn"
    masked = serving.project_facts(rows, mask=True)
    assert masked[0]["resolved_value"].endswith("4399") and "•" in masked[0]["resolved_value"]


def test_sensitivity_for_key():
    assert serving.sensitivity_for_key("id.curp") == "CRITICAL"
    assert serving.sensitivity_for_key("identity.full_name") == "HIGH"
    assert serving.sensitivity_for_key("doc.issue_date") == "LOW"
    assert serving.sensitivity_for_key(None) == "LOW"
