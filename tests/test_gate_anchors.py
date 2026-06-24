"""Unit tests for the Stage-0 anchor gate (di.gate.anchors).

Pure tests - python-stdnum is a light dependency that is always installed, so no skips are needed.
The known-valid IDs below are standard stdnum test vectors (valid checksum / structure).
"""
from __future__ import annotations

from di.gate.anchors import classify_by_anchors, detect_ids
from di.models import Classification

# Known-valid test vectors (verified against python-stdnum validators):
VALID_SSN = "234-56-7890"          # us.ssn structure-valid
VALID_SIN = "193-456-787"          # ca.sin Luhn-valid
VALID_CURP = "SABC560626MDFLRN01"  # mx.curp check-digit valid
VALID_RFC = "GODE561231GR8"        # mx.rfc structure-valid
VALID_EIN = "12-3456789"           # us.ein prefix-valid

# Invalid-checksum counterparts:
BAD_SSN = "078-05-1120"            # us.ssn InvalidComponent
BAD_CURP = "PERJ820123HDFXXX08"    # mx.curp InvalidChecksum


# ---------------------------------------------------------------------------
# classify_by_anchors
# ---------------------------------------------------------------------------
def test_ine_anchors_classify_as_mx_ine() -> None:
    text = (
        "INSTITUTO NACIONAL ELECTORAL\n"
        "CREDENCIAL PARA VOTAR\n"
        "CLAVE DE ELECTOR ABCDEF12345601\n"
        "Nombre: JUAN PEREZ"
    )
    results = classify_by_anchors(text)
    assert results, "expected at least one classification"
    top = results[0]
    assert isinstance(top, Classification)
    assert top.doc_type == "MX_INE"
    assert top.doc_category == "identity"
    assert top.jurisdiction == "MX"
    assert 0.0 < top.confidence <= 1.0
    # Matched anchors are surfaced as signals.
    assert "INSTITUTO NACIONAL ELECTORAL" in top.signals
    assert "CLAVE DE ELECTOR" in top.signals


def test_results_ranked_descending_by_confidence() -> None:
    text = (
        "INSTITUTO NACIONAL ELECTORAL CREDENCIAL PARA VOTAR\n"
        "This is a UTILITY bill mentioning ACCOUNT NUMBER 123."
    )
    results = classify_by_anchors(text)
    confidences = [c.confidence for c in results]
    assert confidences == sorted(confidences, reverse=True)
    # The strong 3-word INE header should outrank the generic utility-bill tokens.
    assert results[0].doc_type == "MX_INE"


def test_empty_or_unmatched_text_returns_empty_list() -> None:
    assert classify_by_anchors("") == []
    assert classify_by_anchors("lorem ipsum dolor sit amet, nothing relevant here") == []


def test_case_insensitive_anchor_matching() -> None:
    text = "instituto nacional electoral / credencial para votar"
    results = classify_by_anchors(text)
    assert any(c.doc_type == "MX_INE" for c in results)


def test_spanish_lang_hint_resolves_multi_jurisdiction_to_mx() -> None:
    # BANK_STATEMENT spans US/CA/MX; a Spanish anchor + es hint should pick MX.
    text = "ESTADO DE CUENTA del cliente"
    results = classify_by_anchors(text, lang="es")
    bank = next((c for c in results if c.doc_type == "BANK_STATEMENT"), None)
    assert bank is not None
    assert bank.jurisdiction == "MX"


# ---------------------------------------------------------------------------
# detect_ids
# ---------------------------------------------------------------------------
def test_detects_valid_ids() -> None:
    text = (
        f"SSN: {VALID_SSN}\n"
        f"SIN: {VALID_SIN}\n"
        f"CURP: {VALID_CURP}\n"
        f"RFC: {VALID_RFC}\n"
        f"EIN: {VALID_EIN}\n"
        "MRZ: P<USAPEREZ<<JUAN<<<<<<<<<<<<<<<<<<<<"
    )
    ids = detect_ids(text)
    # Normalized (compact) forms come back from stdnum.
    assert ids["ssn"] == ["234567890"]
    assert ids["sin"] == ["193456787"]
    assert ids["curp"] == [VALID_CURP]
    assert ids["rfc"] == [VALID_RFC]
    assert ids["ein"] == ["123456789"]
    assert ids["passport_mrz"] == ["P<USA"]


def test_invalid_checksums_are_rejected() -> None:
    ids = detect_ids(f"SSN {BAD_SSN} and CURP {BAD_CURP}")
    assert "ssn" not in ids
    assert "curp" not in ids
    assert ids == {}


def test_curp_does_not_leak_into_rfc_bucket() -> None:
    # A bare CURP must not be mis-detected as an RFC (overlapping prefixes).
    ids = detect_ids(f"CURP {VALID_CURP}")
    assert ids.get("curp") == [VALID_CURP]
    assert "rfc" not in ids


def test_mrz_requires_recognised_issuing_nation() -> None:
    # "P<XXX" is structurally MRZ-shaped but not a recognised KYC nation -> dropped.
    ids = detect_ids("Header line: P<XXXDOE<<JOHN<<<<<<<<<<<<<<<<<<<<<<")
    assert "passport_mrz" not in ids


def test_empty_text_returns_empty_dict() -> None:
    assert detect_ids("") == {}


def test_no_empty_id_lists_emitted() -> None:
    ids = detect_ids(f"only a valid ssn here {VALID_SSN}")
    assert ids == {"ssn": ["234567890"]}
    assert all(v for v in ids.values())


def test_deduplicates_repeated_ids() -> None:
    text = f"{VALID_SSN} appears twice: {VALID_SSN}"
    ids = detect_ids(text)
    assert ids["ssn"] == ["234567890"]
