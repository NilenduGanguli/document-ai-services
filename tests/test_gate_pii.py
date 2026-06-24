"""Unit tests for the PII gate (``di.gate.pii``).

The fallback (regex + stdnum) path runs everywhere — it is the default when Presidio / spaCy are
absent, which is the case in CI without the ``[ml]`` extras. The Presidio path is guarded behind
``importorskip`` so it skips cleanly when the dependency is missing.
"""
from __future__ import annotations

import pytest

from di.gate import pii as pii_mod
from di.models import LangProfile, LangSpan, SensitivityBucket

# Valid fixtures (verified against python-stdnum checksums):
VALID_SSN = "536-90-4399"
VALID_CURP = "SABC560626MDFLRN01"
VALID_RFC = "VECA871012X29"
VALID_SIN = "123-456-782"  # 123456782 passes the SIN Luhn check
VALID_EIN = "12-3456789"


def _profile(lang: str = "en", spans: list[LangSpan] | None = None) -> LangProfile:
    return LangProfile(dominant_lang=lang, is_bilingual=False, spans=spans or [])


def test_fallback_ssn_and_curp_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core requirement: a valid SSN + a valid CURP -> entities present and CRITICAL bucket.

    Force the fallback path even if Presidio happens to be installed by stubbing the analyzer
    builder to ``None``.
    """
    monkeypatch.setattr(pii_mod, "_build_analyzer", lambda: None)

    text = f"Empleado SSN {VALID_SSN} y su CURP es {VALID_CURP} segun el RENAPO."
    entities, bucket = pii_mod.scan_pii(text, _profile("es"))

    types = {e.entity_type for e in entities}
    assert "US_SSN" in types
    assert "MX_CURP" in types
    assert bucket is SensitivityBucket.critical

    # Offsets must map back to the originals.
    by_type = {e.entity_type: e for e in entities}
    assert text[by_type["US_SSN"].start : by_type["US_SSN"].end] == VALID_SSN
    assert text[by_type["MX_CURP"].start : by_type["MX_CURP"].end] == VALID_CURP


def test_fallback_invalid_curp_not_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CURP-shaped string that fails the stdnum checksum is dropped."""
    monkeypatch.setattr(pii_mod, "_build_analyzer", lambda: None)

    text = "CURP: VECA871012HMNAGS00 (bad checksum)"
    entities, bucket = pii_mod.scan_pii(text, _profile("es"))

    assert all(e.entity_type != "MX_CURP" for e in entities)
    assert bucket is SensitivityBucket.low


def test_fallback_curp_not_mistagged_as_rfc(monkeypatch: pytest.MonkeyPatch) -> None:
    """A standalone valid CURP must not also be emitted as an overlapping RFC."""
    monkeypatch.setattr(pii_mod, "_build_analyzer", lambda: None)

    text = f"CURP {VALID_CURP}"
    entities, _ = pii_mod.scan_pii(text, _profile("es"))

    curp_spans = [(e.start, e.end) for e in entities if e.entity_type == "MX_CURP"]
    rfc_spans = [(e.start, e.end) for e in entities if e.entity_type == "MX_RFC"]
    assert len(curp_spans) == 1
    # No RFC entity should overlap the CURP span.
    for rs, re_ in rfc_spans:
        for cs, ce in curp_spans:
            assert not (rs < ce and re_ > cs)


def test_fallback_valid_rfc_sin_ein_are_national_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pii_mod, "_build_analyzer", lambda: None)

    text = f"RFC {VALID_RFC}; SIN {VALID_SIN}; EIN {VALID_EIN}."
    entities, bucket = pii_mod.scan_pii(text, _profile("en"))
    types = {e.entity_type for e in entities}

    assert "MX_RFC" in types
    assert "CA_SIN" in types
    assert "US_EIN" in types
    assert bucket is SensitivityBucket.critical


def test_fallback_lone_email_is_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pii_mod, "_build_analyzer", lambda: None)

    text = "Contact: jane.doe@example.com for details."
    entities, bucket = pii_mod.scan_pii(text, _profile("en"))
    types = {e.entity_type for e in entities}

    assert "EMAIL_ADDRESS" in types
    assert bucket is SensitivityBucket.low


def test_fallback_lone_phone_is_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pii_mod, "_build_analyzer", lambda: None)

    text = "Call me at 415-555-0182 tomorrow."
    entities, bucket = pii_mod.scan_pii(text, _profile("en"))
    types = {e.entity_type for e in entities}

    assert "PHONE_NUMBER" in types
    assert bucket is SensitivityBucket.low


def test_empty_text_returns_low() -> None:
    entities, bucket = pii_mod.scan_pii("", _profile("en"))
    assert entities == []
    assert bucket is SensitivityBucket.low


def test_score_sensitivity_person_dob_address() -> None:
    from di.models import PiiEntity

    person_only = [PiiEntity(entity_type="PERSON", start=0, end=4, score=0.9)]
    assert pii_mod.score_sensitivity(person_only) is SensitivityBucket.low

    person_dob = [
        PiiEntity(entity_type="PERSON", start=0, end=4, score=0.9),
        PiiEntity(entity_type="DATE_TIME", start=5, end=15, score=0.8),
    ]
    assert pii_mod.score_sensitivity(person_dob) is SensitivityBucket.medium

    person_dob_addr = person_dob + [
        PiiEntity(entity_type="LOCATION", start=16, end=30, score=0.7)
    ]
    assert pii_mod.score_sensitivity(person_dob_addr) is SensitivityBucket.high


def test_score_sensitivity_national_id_wins() -> None:
    from di.models import PiiEntity

    mixed = [
        PiiEntity(entity_type="EMAIL_ADDRESS", start=0, end=10, score=0.8),
        PiiEntity(entity_type="US_SSN", start=11, end=22, score=0.95),
    ]
    assert pii_mod.score_sensitivity(mixed) is SensitivityBucket.critical


def test_ine_clave_with_spanish_context_scores_higher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pii_mod, "_build_analyzer", lambda: None)

    clave = "ABCDEF12345678H901"  # 6 letters + 8 digits + H + 3 digits
    with_ctx = f"Clave de Elector: {clave}"
    entities, bucket = pii_mod.scan_pii(with_ctx, _profile("es"))
    by_type = {e.entity_type: e for e in entities}
    assert "MX_INE_CLAVE_ELECTOR" in by_type
    assert by_type["MX_INE_CLAVE_ELECTOR"].score >= 0.8
    assert bucket is SensitivityBucket.critical


def test_presidio_path_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Presidio is installed, the analyzer build should not raise. Skips otherwise."""
    pytest.importorskip("presidio_analyzer")
    # Build may still return None if spaCy models are absent; that is acceptable.
    engine = pii_mod._build_analyzer()
    text = f"My SSN is {VALID_SSN}."
    entities, bucket = pii_mod.scan_pii(text, _profile("en"))
    # Regardless of which path ran, we must get a well-formed result.
    assert isinstance(entities, list)
    assert isinstance(bucket, SensitivityBucket)
    if engine is None:
        # Fell back to regex sweep -> SSN should be CRITICAL.
        assert bucket is SensitivityBucket.critical
