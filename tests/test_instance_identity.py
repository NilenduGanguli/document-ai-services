"""Unit tests for multi-valued-fact instance identity (di.subtree.merge + di.ontology).

Pure-logic — no DB, no network. Covers cardinality declaration, the deterministic value-fingerprint
that groups candidates into instances, per-instance conflict/adjudication, and the accent-folding
fix that keeps identity fingerprinting and within-instance conflict detection consistent (an
accented and unaccented spelling of the same name must merge into ONE instance with conflict=False,
not two instances, and not one instance permanently flagged conflict=True).
"""
from __future__ import annotations

from datetime import date

from di.ontology import MULTI_VALUED_ATTRIBUTE_KEYS, cardinality_for
from di.subtree.merge import (
    Adjudication,
    FactInput,
    _identity_basis,
    _normalize_identity,
    instance_fingerprint,
    merge_facts,
)


# ---------------------------------------------------------------------------
# cardinality_for
# ---------------------------------------------------------------------------
def test_cardinality_for_listed_keys_is_multi():
    for key in MULTI_VALUED_ATTRIBUTE_KEYS:
        assert cardinality_for(key) == "multi"


def test_cardinality_for_unknown_key_defaults_single():
    assert cardinality_for("identity.full_name") == "single"
    assert cardinality_for("some.made.up.key") == "single"


# ---------------------------------------------------------------------------
# Fingerprint determinism + accent/case/whitespace equivalence
# ---------------------------------------------------------------------------
def test_fingerprint_is_deterministic_across_runs():
    normalized = _normalize_identity("Juan Pérez Gómez")
    assert instance_fingerprint(normalized) == instance_fingerprint(normalized)


def test_fingerprint_accent_case_whitespace_variants_are_equal():
    a = _normalize_identity("Juan Pérez Gómez")
    b = _normalize_identity("JUAN  PEREZ GOMEZ")
    assert a == b
    assert instance_fingerprint(a) == instance_fingerprint(b)


def test_fingerprint_distinct_names_are_distinct():
    a = instance_fingerprint(_normalize_identity("Juan Perez"))
    b = instance_fingerprint(_normalize_identity("Maria Lopez"))
    assert a != b


def test_fingerprint_hmac_key_changes_output_deterministically():
    normalized = _normalize_identity("Juan Perez")
    plain = instance_fingerprint(normalized)
    hmac1 = instance_fingerprint(normalized, "deployment-secret-1")
    hmac2 = instance_fingerprint(normalized, "deployment-secret-2")
    assert plain != hmac1 != hmac2
    assert instance_fingerprint(normalized, "deployment-secret-1") == hmac1


def test_identity_basis_prefers_text_then_date_then_num():
    assert _identity_basis(FactInput(fact_id="a", attribute_key="k", value="X")) == "X"
    assert _identity_basis(
        FactInput(fact_id="a", attribute_key="k", value_date=date(2020, 1, 1))
    ) == "2020-01-01"
    assert _identity_basis(FactInput(fact_id="a", attribute_key="k", value_num=42.5)) == "42.5"
    assert _identity_basis(FactInput(fact_id="a", attribute_key="k")) is None


# ---------------------------------------------------------------------------
# Acta Constitutiva scenario — the design's motivating case
# ---------------------------------------------------------------------------
def test_acta_constitutiva_three_directors():
    facts = [
        FactInput(fact_id="d1", attribute_key="ownership.director", value="Juan Perez",
                  confidence=0.9),
        FactInput(fact_id="d2", attribute_key="ownership.director", value="Maria Lopez",
                  confidence=0.85),
        FactInput(fact_id="d3", attribute_key="ownership.director", value="Carlos Ruiz",
                  confidence=0.8),
    ]
    result = merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    assert len(result) == 3
    assert all(f.attribute_key == "ownership.director" for f in result)
    assert len({f.instance_key for f in result}) == 3
    assert all(f.conflict is False and f.needs_review is False for f in result)
    assert all(f.resolution_rationale["instance_count"] == 3 for f in result)
    names = {f.resolved_value for f in result}
    assert names == {"Juan Perez", "Maria Lopez", "Carlos Ruiz"}


def test_same_director_two_documents_one_instance_max_confidence_wins():
    facts = [
        FactInput(fact_id="doc1", attribute_key="ownership.director", value="Juan Perez",
                  confidence=0.6),
        FactInput(fact_id="doc2", attribute_key="ownership.director", value="Juan Perez",
                  confidence=0.95),
    ]
    result = merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    assert len(result) == 1
    fact = result[0]
    assert fact.confidence == 0.95
    assert set(fact.source_fact_ids) == {"doc1", "doc2"}
    assert fact.resolution_rationale["instance_count"] == 1


def test_within_instance_secondary_conflict_on_date():
    """Same director (same fingerprint), but two documents assert different appointment dates."""
    facts = [
        FactInput(fact_id="doc1", attribute_key="ownership.director", value="Juan Perez",
                  value_date=date(2020, 1, 1), confidence=0.7),
        FactInput(fact_id="doc2", attribute_key="ownership.director", value="Juan Perez",
                  value_date=date(2021, 6, 1), confidence=0.9),
    ]
    result = merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    assert len(result) == 1
    fact = result[0]
    assert fact.conflict is True
    assert fact.needs_review is True
    assert fact.value_date == date(2021, 6, 1)  # winner is still max-confidence


def test_accent_variant_merges_to_one_instance_no_conflict():
    """The corrected-design fix: identity fingerprinting AND within-instance conflict detection
    use the same accent-folding normalization, so an Acta's accented spelling and an OCR'd INE's
    unaccented spelling of the same director merge into ONE instance with conflict=False — not
    two instances, and not one instance permanently flagged conflict=True."""
    facts = [
        FactInput(fact_id="acta", attribute_key="ownership.director", value="Juan Pérez",
                  confidence=0.9),
        FactInput(fact_id="ine", attribute_key="ownership.director", value="Juan Perez",
                  confidence=0.7),
    ]
    result = merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    assert len(result) == 1
    fact = result[0]
    assert fact.conflict is False
    assert fact.needs_review is False
    assert set(fact.source_fact_ids) == {"acta", "ine"}


def test_empty_value_candidates_excluded_under_multi_key():
    facts = [
        FactInput(fact_id="empty", attribute_key="ownership.director", value=None, confidence=0.5),
        FactInput(fact_id="real", attribute_key="ownership.director", value="Juan Perez",
                  confidence=0.9),
    ]
    result = merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    assert len(result) == 1
    assert result[0].source_fact_ids == ["real"]


def test_empty_value_candidates_still_behave_normally_under_single_key():
    """Same empty-candidate shape, but the key is NOT multi-valued — unaffected by the exclusion,
    matches the existing single-key None-handling behavior."""
    facts = [
        FactInput(fact_id="empty", attribute_key="identity.surname", value=None, confidence=0.2),
        FactInput(fact_id="real", attribute_key="identity.surname", value="Garcia", confidence=0.9),
    ]
    result = merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    assert len(result) == 1
    assert set(result[0].source_fact_ids) == {"empty", "real"}


# ---------------------------------------------------------------------------
# Per-instance adjudication
# ---------------------------------------------------------------------------
def _three_directors():
    return [
        FactInput(fact_id="d1", attribute_key="ownership.director", value="Juan Perez",
                  confidence=0.9),
        FactInput(fact_id="d2", attribute_key="ownership.director", value="Maria Lopez",
                  confidence=0.85),
        FactInput(fact_id="d3", attribute_key="ownership.director", value="Carlos Ruiz",
                  confidence=0.8),
    ]


def test_accept_one_instance_others_untouched():
    facts = _three_directors()
    baseline = {f.resolved_value: f.instance_key for f in
               merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)}
    key_b = baseline["Maria Lopez"]
    adjudications = {
        ("ownership.director", key_b): Adjudication(
            attribute_key="ownership.director", instance_key=key_b, verdict="accept",
            reviewer="reviewer-1"),
    }
    result = merge_facts(facts, adjudications=adjudications, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    assert len(result) == 3
    b = next(f for f in result if f.instance_key == key_b)
    assert b.adjudicated is True
    assert b.verification_status.value == "human_verified"
    others = [f for f in result if f.instance_key != key_b]
    assert all(not f.adjudicated for f in others)


def test_override_one_instance_keeps_instance_key():
    """An override changes the displayed value but instance_key is a sticky identity handle —
    it is NOT re-derived from the overridden value."""
    facts = _three_directors()
    baseline = {f.resolved_value: f.instance_key for f in
               merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)}
    key_b = baseline["Maria Lopez"]
    adjudications = {
        ("ownership.director", key_b): Adjudication(
            attribute_key="ownership.director", instance_key=key_b, verdict="override",
            value_text="Maria Lopez Hernandez", reviewer="reviewer-1"),
    }
    result = merge_facts(facts, adjudications=adjudications, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    b = next(f for f in result if f.instance_key == key_b)
    assert b.resolved_value == "Maria Lopez Hernandez"
    assert b.instance_key == key_b  # unchanged despite the value change


def test_reject_one_instance_row_absent_others_present():
    """Reject on a multi-valued instance removes the row entirely — 'remove a spurious director'
    — unlike a single-key reject, which keeps the row nulled and flagged."""
    facts = _three_directors()
    baseline = {f.resolved_value: f.instance_key for f in
               merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)}
    key_c = baseline["Carlos Ruiz"]
    adjudications = {
        ("ownership.director", key_c): Adjudication(
            attribute_key="ownership.director", instance_key=key_c, verdict="reject",
            reviewer="reviewer-1"),
    }
    result = merge_facts(facts, adjudications=adjudications, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    assert len(result) == 2
    values = {f.resolved_value for f in result}
    assert values == {"Juan Perez", "Maria Lopez"}


def test_reject_is_deterministic_across_remerges():
    """Because fingerprints are deterministic, a re-merge (same source facts) reproduces the same
    instance_key for the rejected instance and drops it again — the reject 'survives' re-merge."""
    facts = _three_directors()
    baseline = {f.resolved_value: f.instance_key for f in
               merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)}
    key_c = baseline["Carlos Ruiz"]
    adjudications = {
        ("ownership.director", key_c): Adjudication(
            attribute_key="ownership.director", instance_key=key_c, verdict="reject"),
    }
    first = merge_facts(facts, adjudications=adjudications, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    second = merge_facts(facts, adjudications=adjudications, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    assert {f.resolved_value for f in first} == {f.resolved_value for f in second} == \
        {"Juan Perez", "Maria Lopez"}


def test_output_sorted_by_attribute_key_then_instance_key():
    facts = _three_directors() + [
        FactInput(fact_id="a1", attribute_key="account.number", value="ACC-001", confidence=0.9),
    ]
    result = merge_facts(facts, multi_keys=MULTI_VALUED_ATTRIBUTE_KEYS)
    keys = [(f.attribute_key, f.instance_key) for f in result]
    assert keys == sorted(keys)
