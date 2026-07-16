"""Unit tests for the serving layer (di.serving) — pure, no DB/network."""
from __future__ import annotations

from datetime import date

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


def test_ownership_keys_are_high_sensitivity():
    """Beneficial-owner and director identities are personal data — must be masked by default,
    same as any other HIGH-sensitivity attribute."""
    assert serving.sensitivity_for_key("ownership.director") == "HIGH"
    assert serving.sensitivity_for_key("ownership.beneficial_owner") == "HIGH"


def test_project_facts_instance_count_multiple_directors():
    rows = [
        {"attribute_key": "ownership.director", "instance_key": "aaaa", "resolved_value": "Juan",
         "confidence": 0.9, "conflict": False, "verification_status": "checksum_verified"},
        {"attribute_key": "ownership.director", "instance_key": "bbbb", "resolved_value": "Maria",
         "confidence": 0.9, "conflict": False, "verification_status": "checksum_verified"},
        {"attribute_key": "ownership.director", "instance_key": "cccc", "resolved_value": "Carlos",
         "confidence": 0.9, "conflict": False, "verification_status": "checksum_verified"},
        {"attribute_key": "id.ssn", "instance_key": "", "resolved_value": "536-90-4399",
         "confidence": 0.9, "conflict": False, "verification_status": "checksum_verified"},
    ]
    out = serving.project_facts(rows)
    directors = [f for f in out if f["attribute_key"] == "ownership.director"]
    ssn = next(f for f in out if f["attribute_key"] == "id.ssn")
    assert all(f["instance_count"] == 3 for f in directors)
    assert ssn["instance_count"] == 1


def test_project_facts_instance_count_computed_before_verified_only_filter():
    """instance_count must reflect ALL instances, not just the ones that survive verified_only —
    otherwise a client with 2/3 verified directors would misreport 'instance_count: 2'."""
    rows = [
        {"attribute_key": "ownership.director", "instance_key": "aaaa", "resolved_value": "Juan",
         "confidence": 0.9, "conflict": False, "verification_status": "checksum_verified"},
        {"attribute_key": "ownership.director", "instance_key": "bbbb", "resolved_value": "Maria",
         "confidence": 0.4, "conflict": False, "verification_status": "llm_unverified"},
    ]
    out = serving.project_facts(rows, verified_only=True)
    assert len(out) == 1
    assert out[0]["instance_count"] == 2


def test_project_facts_redacts_identity_basis_under_mask():
    """resolution_rationale.identity_basis carries the cleartext normalized value (a director's
    name) an instance was fingerprinted from — it must be redacted whenever mask hides the rest of
    the row, or a masked response still leaks the identity in cleartext via the rationale."""
    rows = [{
        "attribute_key": "ownership.director", "instance_key": "aaaa", "resolved_value": "Juan Perez",
        "confidence": 0.9, "conflict": False, "verification_status": "checksum_verified",
        "resolution_rationale": {"identity_basis": "juan perez", "identity_algo": "nfkd-casefold-ws-v1"},
    }]
    masked = serving.project_facts(rows, mask=True)
    assert masked[0]["resolution_rationale"]["identity_basis"] == "***"
    clear = serving.project_facts(rows, mask=False)
    assert clear[0]["resolution_rationale"]["identity_basis"] == "juan perez"


def test_masking_redacts_date_and_num_not_just_text():
    """A masked DOB must not leak via value_date (regression: UI showed `date: 1985-03-12`)."""
    rows = [{
        "attribute_key": "identity.date_of_birth", "resolved_value": "1985-03-12",
        "value_date": date(1985, 3, 12), "value_num": None, "confidence": 0.9,
        "conflict": False, "verification_status": "checksum_verified",
    }, {
        "attribute_key": "income.annual", "resolved_value": "250000",
        "value_date": None, "value_num": 250000.0, "confidence": 0.9,
        "conflict": False, "verification_status": "checksum_verified",
    }]
    out = serving.project_facts(rows, mask=True)
    dob, income = out[0], out[1]
    assert dob["masked"] is True
    assert dob["value_date"] is None, "masked DOB leaked through value_date"
    assert income["value_num"] is None, "masked income leaked through value_num"
    # unmasked still carries the real values
    clear = serving.project_facts(rows, mask=False)
    assert clear[0]["value_date"] == date(1985, 3, 12)
    assert clear[1]["value_num"] == 250000.0


def test_masking_node_redacts_date_and_num():
    """Same leak on the tree projection."""
    rows = [{
        "id": "n1", "parent_id": None, "node_type": "fact", "path": "a", "seq": 0, "depth": 1,
        "attribute_key": "identity.date_of_birth", "value_text": "1985-03-12",
        "value_date": date(1985, 3, 12), "value_num": 42.0, "sensitivity": "LOW",
        "confidence": 0.9, "verification_status": "checksum_verified", "provenance": {},
    }]
    node = serving.nest_tree(rows, mask=True)[0]
    assert node["masked"] is True
    assert node["value_date"] is None and node["value_num"] is None


def test_document_sensitivity_raises_gate_verdict_to_extracted_facts():
    """A passport must not stay LOW just because the optional PII model never ran.

    Regression: the lean container ships without [ml], so the gate scored every document LOW —
    and a PASSPORT was stored as LOW sensitivity with its chunk text served unmasked.
    """
    assert serving.document_sensitivity("LOW", ["id.passport_number"]) == "CRITICAL"
    assert serving.document_sensitivity("LOW", ["identity.surname"]) == "HIGH"
    assert serving.document_sensitivity("LOW", ["doc.expiry_date"]) == "LOW"
    # never downgrades the gate's own verdict
    assert serving.document_sensitivity("CRITICAL", ["doc.expiry_date"]) == "CRITICAL"
    assert serving.document_sensitivity("HIGH", []) == "HIGH"
    # takes the max across a mixed set
    assert serving.document_sensitivity(
        "LOW", ["doc.expiry_date", "identity.surname", "id.passport_number"]) == "CRITICAL"
