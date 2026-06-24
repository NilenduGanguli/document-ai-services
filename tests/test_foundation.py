"""Foundation tests — config, models, ontology integrity, db helpers, stub gateway.

Pure-logic (no DB / no network). Guards the contracts that every module builds against.
"""
from __future__ import annotations

import pytest

from di import ontology
from di.config import get_settings
from di.db import vec_to_pg
from di.models import ExtractedField, GateDecision, NodeType, VerificationStatus
from di.retrieval_client import StubRetrievalClient


def test_settings_load():
    s = get_settings()
    assert s.pg_schema
    assert "en" in s.supported_languages and "es" in s.supported_languages
    assert set(s.supported_jurisdictions) == {"US", "CA", "MX"}


def test_vec_to_pg_format():
    assert vec_to_pg([0.1, 0.2, 0.3]) == "[0.1,0.2,0.3]"
    assert vec_to_pg([]) == "[]"


def test_models_enums_and_field():
    f = ExtractedField(attribute_key="identity.date_of_birth", value="1990-01-01", confidence=0.9)
    assert f.verification_status == VerificationStatus.unverified
    assert NodeType.fact.value == "fact"
    assert GateDecision.deterministic_only.value == "DETERMINISTIC_ONLY"


def test_ontology_attribute_keys_referenced_exist():
    """Every attribute_key referenced by a doc-type must be defined in the catalog."""
    for spec in ontology.DOC_TYPES:
        for key in spec.attribute_keys:
            assert key in ontology.ATTRIBUTE_KEYS, f"{spec.code} references unknown key {key}"


def test_ontology_jurisdictions_in_scope():
    allowed = {"US", "CA", "MX"}
    for spec in ontology.DOC_TYPES:
        assert set(spec.jurisdictions) <= allowed, spec.code
        assert spec.jurisdictions, f"{spec.code} has no jurisdiction"


def test_ontology_anchors_present_per_language():
    en = ontology.anchors_for("en")
    es = ontology.anchors_for("es")
    # Mexican docs must carry Spanish anchors; US/CA core docs must carry English anchors.
    assert "MX_CURP" in es and "MX_INE" in es
    assert "US_SSN_CARD" in en and "PASSPORT" in en


def test_ontology_deterministic_set_includes_checksummed_ids():
    det = ontology.deterministic_doc_types()
    for code in ("PASSPORT", "US_SSN_CARD", "CA_SIN", "MX_CURP", "MX_RFC_CSF", "MX_INE"):
        assert code in det, code


@pytest.mark.asyncio
async def test_stub_gateway_embed_and_complete():
    client = StubRetrievalClient(get_settings())
    dim = get_settings().embedding_dim_default
    vecs = await client.embed(["hello", "world"])
    assert len(vecs) == 2 and len(vecs[0]) == dim
    # deterministic: same text -> same vector
    again = await client.embed(["hello"])
    assert again[0] == vecs[0]
    text, usage = await client.llm_complete([{"role": "user", "content": "classify this"}], task="fast")
    assert "stub" in text and "total" in usage
