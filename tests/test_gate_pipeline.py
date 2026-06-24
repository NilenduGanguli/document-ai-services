"""Unit tests for the gate pipeline orchestration (``di.gate.pipeline.run_gate``).

Offline by design: no optional ML deps are required, so every sub-stage exercises its deterministic
fallback (lingua -> stopword heuristic, scikit-learn -> anchor classifier, Presidio -> regex+stdnum
sweep). The two anchor scenarios are the load-bearing ones from the module contract:

* A Mexican INE credential header + a *checksum-valid* CURP -> CRITICAL sensitivity and a fail-safe
  DETERMINISTIC_ONLY decision, classified to an MX doc-type.
* A benign English W-2 with clear anchors and the gate open -> LOW sensitivity and SEND_TO_LLM.
"""
from __future__ import annotations

import pytest

from di.config import get_settings
from di.gate.pipeline import run_gate
from di.models import GateDecision, GateResult, OcrResult, SensitivityBucket

# A CURP that passes python-stdnum's CURP checksum (so the regex+stdnum PII fallback tags it as a
# validated MX national ID -> CRITICAL). Second char is a vowel so it also clears the fallback regex.
VALID_CURP = "HEGG560427MVZRRL04"


def _ocr(text: str) -> OcrResult:
    return OcrResult(engine="stub", pages=1, text=text)


@pytest.fixture
def settings():
    get_settings.cache_clear()
    return get_settings()


def test_mexican_ine_curp_is_critical_and_deterministic_only(settings) -> None:
    """INE header + a valid CURP must be CRITICAL and stay on the deterministic-only path."""
    text = f"INSTITUTO NACIONAL ELECTORAL CLAVE DE ELECTOR CURP {VALID_CURP}"
    result = run_gate(_ocr(text), settings=settings)

    assert isinstance(result, GateResult)
    assert result.sensitivity == SensitivityBucket.critical
    assert result.decision == GateDecision.deterministic_only
    # The Spanish INE anchors must classify this to a Mexican doc-type.
    assert result.classification.jurisdiction == "MX"
    assert result.lang_profile.dominant_lang == "es"
    # The validated CURP should surface as a national-ID PII entity.
    assert any("CURP" in e.entity_type for e in result.pii_entities)
    assert result.rationale  # non-empty audit string


def test_benign_english_anchor_sends_to_llm_when_gate_open(settings) -> None:
    """A confidently-classified, LOW-sensitivity English doc with the gate open goes to the LLM."""
    assert settings.gate_default_open is True
    text = "Form W-2 Wage and Tax Statement. OMB No. 1545-0008. Reporting period summary."
    result = run_gate(_ocr(text), settings=settings)

    assert result.lang_profile.dominant_lang == "en"
    assert result.sensitivity == SensitivityBucket.low
    assert result.classification.confidence >= settings.classifier_confidence_floor
    assert result.decision == GateDecision.send_to_llm
    assert not result.pii_entities


def test_run_gate_defaults_settings_to_get_settings() -> None:
    """``settings`` is optional and defaults to ``get_settings()`` (still offline-safe)."""
    text = f"INSTITUTO NACIONAL ELECTORAL CLAVE DE ELECTOR CURP {VALID_CURP}"
    result = run_gate(_ocr(text))
    assert result.sensitivity == SensitivityBucket.critical
    assert result.decision == GateDecision.deterministic_only


def test_empty_ocr_text_is_fail_safe_low_and_deterministic(settings) -> None:
    """Empty input must not raise and must fail safe (UNKNOWN -> deterministic-only)."""
    result = run_gate(_ocr(""), settings=settings)
    assert result.sensitivity == SensitivityBucket.low
    assert result.decision == GateDecision.deterministic_only
    assert result.classification.doc_type == "UNKNOWN"
