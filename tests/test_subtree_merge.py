"""Unit tests for the client-level fact merge (di.subtree.merge).

Pure-logic — no DB, no network, no optional dependencies. Covers the resolution policy:
agreement -> no conflict; disagreement -> conflict + higher-confidence winner; all source ids
retained; grouping by attribute_key; date/numeric values; empty/None handling.
"""
from __future__ import annotations

from datetime import date

from di.models import ClientFact
from di.ontology import MULTI_VALUED_ATTRIBUTE_KEYS
from di.subtree.merge import Adjudication, FactInput, merge_facts


def test_agreement_no_conflict():
    """Two facts, same key, same value -> single resolved fact, no conflict."""
    facts = [
        FactInput(fact_id="a", attribute_key="identity.full_name", value="John Smith", confidence=0.7),
        FactInput(fact_id="b", attribute_key="identity.full_name", value="John Smith", confidence=0.9),
    ]
    result = merge_facts(facts)
    assert len(result) == 1
    fact = result[0]
    assert isinstance(fact, ClientFact)
    assert fact.attribute_key == "identity.full_name"
    assert fact.resolved_value == "John Smith"
    assert fact.conflict is False
    assert fact.needs_review is False
    assert fact.confidence == 0.9
    assert set(fact.source_fact_ids) == {"a", "b"}


def test_disagreement_conflict_and_winner():
    """Two facts, same key, DIFFERENT value -> conflict + higher-confidence winner + both ids."""
    facts = [
        FactInput(fact_id="low", attribute_key="identity.date_of_birth", value="1990-01-01", confidence=0.4),
        FactInput(fact_id="high", attribute_key="identity.date_of_birth", value="1991-02-02", confidence=0.95),
    ]
    result = merge_facts(facts)
    assert len(result) == 1
    fact = result[0]
    assert fact.conflict is True
    assert fact.needs_review is True
    # winner is the higher-confidence source regardless of input order
    assert fact.resolved_value == "1991-02-02"
    assert fact.confidence == 0.95
    assert set(fact.source_fact_ids) == {"low", "high"}


def test_grouping_by_attribute_key():
    """Different attribute keys yield independent, sorted ClientFacts."""
    facts = [
        FactInput(fact_id="x", attribute_key="id.ssn", value="123-45-6789", confidence=0.8),
        FactInput(fact_id="y", attribute_key="identity.full_name", value="Jane Roe", confidence=0.6),
        FactInput(fact_id="z", attribute_key="id.ssn", value="123-45-6789", confidence=0.5),
    ]
    result = merge_facts(facts)
    assert [f.attribute_key for f in result] == ["id.ssn", "identity.full_name"]
    ssn = next(f for f in result if f.attribute_key == "id.ssn")
    assert ssn.conflict is False
    assert set(ssn.source_fact_ids) == {"x", "z"}
    assert ssn.confidence == 0.8


def test_string_normalization_not_a_conflict():
    """Cosmetic OCR/whitespace/case differences must not be flagged as a conflict."""
    facts = [
        FactInput(fact_id="a", attribute_key="identity.full_name", value="John  SMITH", confidence=0.5),
        FactInput(fact_id="b", attribute_key="identity.full_name", value="john smith", confidence=0.9),
    ]
    result = merge_facts(facts)
    assert result[0].conflict is False
    assert result[0].resolved_value == "john smith"  # winner's raw value preserved


def test_date_and_numeric_values_carry_through():
    """Winner's typed value_date / value_num are carried onto the ClientFact."""
    facts = [
        FactInput(
            fact_id="d1",
            attribute_key="doc.expiry_date",
            value_date=date(2030, 5, 1),
            confidence=0.9,
        ),
        FactInput(
            fact_id="n1",
            attribute_key="account.balance",
            value_num=1234.56,
            confidence=0.7,
        ),
    ]
    result = merge_facts(facts)
    expiry = next(f for f in result if f.attribute_key == "doc.expiry_date")
    balance = next(f for f in result if f.attribute_key == "account.balance")
    assert expiry.value_date == date(2030, 5, 1)
    assert balance.value_num == 1234.56


def test_numeric_disagreement_is_conflict():
    """Distinct numeric values for the same key conflict; winner is higher confidence."""
    facts = [
        FactInput(fact_id="n1", attribute_key="income.amount", value_num=50000.0, confidence=0.6),
        FactInput(fact_id="n2", attribute_key="income.amount", value_num=52000.0, confidence=0.8),
    ]
    result = merge_facts(facts)
    fact = result[0]
    assert fact.conflict is True
    assert fact.value_num == 52000.0
    assert fact.confidence == 0.8


def test_client_id_stamped():
    facts = [FactInput(fact_id="a", attribute_key="id.curp", value="ABCD010101HDFXYZ09", confidence=0.9)]
    result = merge_facts(facts, client_id="client-42")
    assert result[0].client_id == "client-42"


def test_empty_input():
    assert merge_facts([]) == []


def test_single_fact_no_conflict():
    facts = [FactInput(fact_id="only", attribute_key="id.rfc", value="XAXX010101000", confidence=0.3)]
    result = merge_facts(facts)
    assert len(result) == 1
    assert result[0].conflict is False
    assert result[0].source_fact_ids == ["only"]


def test_none_value_does_not_create_phantom_conflict():
    """A source with no comparable value must not trip the conflict flag against a real value."""
    facts = [
        FactInput(fact_id="empty", attribute_key="identity.surname", value=None, confidence=0.2),
        FactInput(fact_id="real", attribute_key="identity.surname", value="Garcia", confidence=0.9),
    ]
    result = merge_facts(facts)
    fact = result[0]
    assert fact.conflict is False
    assert fact.resolved_value == "Garcia"
    assert set(fact.source_fact_ids) == {"empty", "real"}


# ---------------------------------------------------------------------------
# Golden regression: single-valued attributes must be byte-identical whether multi_keys is empty
# or the real production set — the sentinel design's entire point is zero blast radius for keys
# that were never promoted. Every scenario above uses single-valued keys, so re-running them all
# with the real multi_keys set (instead of the implicit empty default) proves this directly.
# ---------------------------------------------------------------------------
def test_golden_regression_with_real_multi_keys_set():
    facts = [
        FactInput(fact_id="a", attribute_key="identity.full_name", value="John Smith", confidence=0.7),
        FactInput(fact_id="b", attribute_key="identity.full_name", value="John Smith", confidence=0.9),
        FactInput(fact_id="low", attribute_key="identity.date_of_birth", value="1990-01-01",
                  confidence=0.4),
        FactInput(fact_id="high", attribute_key="identity.date_of_birth", value="1991-02-02",
                  confidence=0.95),
    ]
    baseline = merge_facts(facts)
    with_multi = merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    assert [f.model_dump() for f in baseline] == [f.model_dump() for f in with_multi]
    assert all(f.instance_key == "" for f in with_multi)


def test_tuple_keyed_adjudication_on_single_key():
    """adjudications is keyed by (attribute_key, instance_key); '' is the sentinel for single
    keys, matching what di.pipeline._remerge_client_facts now builds from stored rows."""
    facts = [FactInput(fact_id="a", attribute_key="id.curp", value="ABCD010101HDFXYZ09",
                       confidence=0.5)]
    adjudications = {
        ("id.curp", ""): Adjudication(attribute_key="id.curp", instance_key="", verdict="override",
                                      value_text="CORRECTED09", reviewer="reviewer-1"),
    }
    result = merge_facts(facts, adjudications=adjudications)
    assert result[0].resolved_value == "CORRECTED09"
    assert result[0].adjudicated is True
    assert result[0].instance_key == ""


def test_reject_on_single_key_keeps_row_nulled_and_flagged():
    """Single-valued reject is legacy behavior verbatim: row stays, values nulled, needs_review."""
    facts = [FactInput(fact_id="a", attribute_key="income.employer", value="Acme", confidence=0.5)]
    adjudications = {
        ("income.employer", ""): Adjudication(attribute_key="income.employer", verdict="reject",
                                              reviewer="reviewer-1"),
    }
    result = merge_facts(facts, adjudications=adjudications)
    assert len(result) == 1
    fact = result[0]
    assert fact.resolved_value is None
    assert fact.needs_review is True
    assert fact.confidence == 0.0
