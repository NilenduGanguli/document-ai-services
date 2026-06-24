"""Unit tests for the Stage-3 routing-gate policy (``di.gate.routing.route``).

Pure-logic: no DB, no network, no optional deps. Exhaustive-ish decision table covering the
sensitivity buckets, confidence floor, gate switch, and redaction flag, plus the fail-safe
default. All egress (SEND_TO_LLM / REDACT_THEN_SEND) must be deliberate; everything else stays
DETERMINISTIC_ONLY.
"""
from __future__ import annotations

import pytest

from di.gate.routing import route
from di.models import Classification, GateDecision, SensitivityBucket

LOW = SensitivityBucket.low
MEDIUM = SensitivityBucket.medium
HIGH = SensitivityBucket.high
CRITICAL = SensitivityBucket.critical

DET = GateDecision.deterministic_only
REDACT = GateDecision.redact_then_send
LLM = GateDecision.send_to_llm

FLOOR = 0.55


def _cls(doc_type: str = "US_W2", confidence: float = 0.9) -> Classification:
    return Classification(doc_type=doc_type, confidence=confidence)


# (label, doc_type, confidence, sensitivity, gate_open, redact_active) -> expected GateDecision
CASES: list[tuple[str, str, float, SensitivityBucket, bool, bool, GateDecision]] = [
    # --- LOW sensitivity: the only path to the LLM ---
    ("low_confident_open", "US_W2", 0.90, LOW, True, False, LLM),
    ("low_confident_open_at_floor", "US_W2", FLOOR, LOW, True, False, LLM),
    ("low_confident_gate_closed", "US_W2", 0.90, LOW, False, False, DET),
    ("low_lowconf_open", "US_W2", 0.20, LOW, True, False, DET),
    ("low_lowconf_just_below_floor", "US_W2", FLOOR - 0.01, LOW, True, False, DET),
    ("low_unknown_doctype_open", "UNKNOWN", 0.99, LOW, True, False, DET),
    ("low_empty_doctype_open", "", 0.99, LOW, True, False, DET),
    ("low_confident_open_redact_irrelevant", "US_W2", 0.90, LOW, True, True, LLM),
    # --- MEDIUM sensitivity: redact-and-send only when redaction is active ---
    ("medium_redact_active", "US_W2", 0.90, MEDIUM, True, True, REDACT),
    ("medium_redact_inactive", "US_W2", 0.90, MEDIUM, True, False, DET),
    ("medium_redact_active_gate_closed", "US_W2", 0.90, MEDIUM, False, True, REDACT),
    # low-confidence + sensitive -> Rule 1 blocks even with redaction active (highest precedence)
    ("medium_lowconf_redact_active", "US_W2", 0.10, MEDIUM, True, True, DET),
    ("medium_lowconf_redact_inactive", "US_W2", 0.10, MEDIUM, True, False, DET),
    # --- HIGH sensitivity ---
    ("high_redact_inactive", "PASSPORT", 0.95, HIGH, True, False, DET),
    ("high_redact_active", "PASSPORT", 0.95, HIGH, True, True, REDACT),
    ("high_redact_active_gate_closed", "PASSPORT", 0.95, HIGH, False, True, REDACT),
    ("high_lowconf_redact_inactive", "PASSPORT", 0.10, HIGH, True, False, DET),
    # low-confidence + sensitive -> Rule 1 blocks even with redaction active
    ("high_lowconf_redact_active", "PASSPORT", 0.10, HIGH, True, True, DET),
    # --- CRITICAL sensitivity ---
    ("critical_redact_inactive", "US_SSN_CARD", 0.99, CRITICAL, True, False, DET),
    ("critical_redact_active", "US_SSN_CARD", 0.99, CRITICAL, True, True, REDACT),
    ("critical_gate_open_no_redact", "US_SSN_CARD", 0.99, CRITICAL, True, False, DET),
    # low-confidence + sensitive -> Rule 1 blocks even with redaction active
    ("critical_lowconf_redact_active", "US_SSN_CARD", 0.10, CRITICAL, True, True, DET),
]


@pytest.mark.parametrize(
    "label,doc_type,confidence,sensitivity,gate_open,redact_active,expected",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_route_decision_table(
    label: str,
    doc_type: str,
    confidence: float,
    sensitivity: SensitivityBucket,
    gate_open: bool,
    redact_active: bool,
    expected: GateDecision,
) -> None:
    decision, rationale = route(
        _cls(doc_type, confidence),
        sensitivity,
        gate_open=gate_open,
        conf_floor=FLOOR,
        redact_active=redact_active,
    )
    assert decision == expected, f"{label}: got {decision} expected {expected} ({rationale})"
    assert isinstance(rationale, str) and rationale, f"{label}: rationale must be non-empty"


def test_low_confidence_sensitive_blocks_before_redaction() -> None:
    """Rule 1 precedence: unknown/low-confidence on a sensitive doc stays deterministic even
    when redaction is active and sensitivity would otherwise allow REDACT_THEN_SEND."""
    decision, rationale = route(
        _cls("UNKNOWN", 0.10),
        HIGH,
        gate_open=True,
        conf_floor=FLOOR,
        redact_active=True,
    )
    assert decision == GateDecision.deterministic_only
    assert "low-confidence" in rationale


def test_redact_active_defaults_false() -> None:
    """redact_active is keyword-only and defaults to False -> sensitive docs are blocked."""
    decision, _ = route(_cls("PASSPORT", 0.95), HIGH, gate_open=True, conf_floor=FLOOR)
    assert decision == GateDecision.deterministic_only


def test_none_doctype_treated_as_unknown() -> None:
    """A None doc_type (defensive) must be treated as unknown and not reach the LLM."""
    cls = Classification.model_construct(doc_type=None, confidence=0.99)
    decision, _ = route(cls, MEDIUM, gate_open=True, conf_floor=FLOOR, redact_active=False)
    assert decision == GateDecision.deterministic_only


def test_default_is_fail_safe_for_every_bucket() -> None:
    """With the gate closed and no redaction, nothing should ever leave deterministic-only."""
    for bucket in (LOW, MEDIUM, HIGH, CRITICAL):
        decision, _ = route(
            _cls("US_W2", 0.99), bucket, gate_open=False, conf_floor=FLOOR, redact_active=False
        )
        assert decision == GateDecision.deterministic_only, bucket
