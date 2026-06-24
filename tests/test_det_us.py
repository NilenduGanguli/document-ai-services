"""Unit tests for the US deterministic extractor (di.extract.deterministic.us).

Pure / offline. ``python-stdnum`` is a light, always-installed dependency, so the
identifier-sweep paths run unconditionally. The anchored-KV soft-field path is tested by
monkeypatching the (concurrently-authored) sibling ``anchored_kv`` module.
"""
from __future__ import annotations

import pytest

from di.extract import base
from di.extract.base import ExtractionInput
from di.extract.deterministic import us
from di.models import (
    ExtractedField,
    ExtractionSource,
    OcrLine,
    SensitivityBucket,
    VerificationStatus,
)

# Known-good / known-bad fixtures validated against stdnum at author time.
VALID_SSN = "536-90-4399"      # passes stdnum.us.ssn
INVALID_SSN = "000-12-3456"    # area 000 -> invalid
VALID_EIN = "12-3456789"       # passes stdnum.us.ein, NN-NNNNNNN shape
VALID_ITIN = "911-70-1234"     # passes stdnum.us.itin (9xx area, valid group)


def _fields(text: str, doc_type: str = "US_SSN_CARD", lines=None, lang: str = "en"):
    return us.US_EXTRACTOR.extract(ExtractionInput(doc_type, text, lines=lines, lang=lang))


def _by_key(fields, key):
    return [f for f in fields if f.attribute_key == key]


# ---------------------------------------------------------------------------
# Registration / protocol
# ---------------------------------------------------------------------------
def test_registered_for_all_handles():
    expected = {"US_SSN_CARD", "US_EIN_LETTER", "US_W2", "US_1099", "US_DRIVER_LICENSE"}
    assert us.US_EXTRACTOR.handles == frozenset(expected)
    for code in expected:
        assert base.get_extractor(code) is us.US_EXTRACTOR


def test_protocol_shape():
    assert isinstance(us.US_EXTRACTOR, base.DeterministicExtractor)


# ---------------------------------------------------------------------------
# SSN — valid vs invalid (stdnum checksum/structure)
# ---------------------------------------------------------------------------
def test_valid_ssn_extracted_and_checksummed():
    fields = _fields(f"SOCIAL SECURITY {VALID_SSN}")
    ssn_fields = _by_key(fields, "id.ssn")
    assert len(ssn_fields) == 1
    f = ssn_fields[0]
    assert isinstance(f, ExtractedField)
    assert f.value == VALID_SSN
    assert f.raw_ocr == VALID_SSN
    assert f.checksum_ok is True
    assert f.verification_status == VerificationStatus.checksum_verified
    assert f.source == ExtractionSource.regex_sweep
    assert f.sensitivity == SensitivityBucket.critical
    assert f.confidence > 0.0


def test_invalid_ssn_rejected():
    fields = _fields(f"SSN {INVALID_SSN}")
    assert _by_key(fields, "id.ssn") == []
    assert _by_key(fields, "id.itin") == []


def test_ssn_deduplicated():
    fields = _fields(f"{VALID_SSN} ... again {VALID_SSN}")
    assert len(_by_key(fields, "id.ssn")) == 1


# ---------------------------------------------------------------------------
# EIN — NN-NNNNNNN distinguished from SSN's NNN-NN-NNNN
# ---------------------------------------------------------------------------
def test_valid_ein_extracted():
    fields = _fields(f"EMPLOYER IDENTIFICATION NUMBER {VALID_EIN}", doc_type="US_EIN_LETTER")
    ein_fields = _by_key(fields, "id.ein")
    assert len(ein_fields) == 1
    f = ein_fields[0]
    assert f.attribute_key == "id.ein"
    assert f.value == VALID_EIN
    assert f.source == ExtractionSource.regex_sweep
    assert f.verification_status == VerificationStatus.checksum_verified
    assert f.sensitivity == SensitivityBucket.high


def test_ein_shape_not_misread_as_ssn():
    """NN-NNNNNNN must never surface as an SSN/ITIN, and vice-versa."""
    fields = _fields(VALID_EIN, doc_type="US_W2")
    assert _by_key(fields, "id.ssn") == []
    assert _by_key(fields, "id.itin") == []
    assert len(_by_key(fields, "id.ein")) == 1


def test_ssn_shape_not_misread_as_ein():
    fields = _fields(VALID_SSN)
    assert _by_key(fields, "id.ein") == []
    assert len(_by_key(fields, "id.ssn")) == 1


def test_ssn_and_ein_coexist_on_w2():
    text = f"Employee SSN {VALID_SSN}  Employer EIN {VALID_EIN}"
    fields = _fields(text, doc_type="US_W2")
    assert len(_by_key(fields, "id.ssn")) == 1
    assert len(_by_key(fields, "id.ein")) == 1


# ---------------------------------------------------------------------------
# ITIN — 9xx area routed to id.itin, not id.ssn
# ---------------------------------------------------------------------------
def test_itin_routed_to_itin_key():
    fields = _fields(f"ITIN {VALID_ITIN}", doc_type="US_1099")
    assert len(_by_key(fields, "id.itin")) == 1
    assert _by_key(fields, "id.ssn") == []
    itin = _by_key(fields, "id.itin")[0]
    assert itin.value == VALID_ITIN
    assert itin.verification_status == VerificationStatus.checksum_verified


# ---------------------------------------------------------------------------
# Anchored-KV soft fields — shared helper patched on the real module
# ---------------------------------------------------------------------------
def test_anchored_kv_absent_is_graceful(monkeypatch):
    """If anchor_extract is missing, identifiers still come back and soft fields are empty."""
    import di.extract.deterministic.anchored_kv as akv

    monkeypatch.delattr(akv, "anchor_extract", raising=False)
    lines = [OcrLine(text="Employer Acme Corp", page=1)]
    fields = _fields(f"EIN {VALID_EIN}", doc_type="US_W2", lines=lines)
    assert len(_by_key(fields, "id.ein")) == 1
    assert _by_key(fields, "income.employer") == []


def test_anchored_kv_invoked_for_soft_keys(monkeypatch):
    """The helper is called with the doc-type's label synonyms; (label, line) pairs map to fields."""
    import di.extract.deterministic.anchored_kv as akv

    captured: dict[str, object] = {}

    def fake_anchor_extract(lines, labels, *, fuzz_threshold=85):
        captured["labels"] = tuple(labels)
        return [("Employer", OcrLine(text="Acme Corp", page=1))]

    monkeypatch.setattr(akv, "anchor_extract", fake_anchor_extract)

    lines = [OcrLine(text="Employer Acme Corp", page=1)]
    fields = _fields(f"Wages EIN {VALID_EIN}", doc_type="US_W2", lines=lines, lang="en")
    emp = _by_key(fields, "income.employer")
    assert len(emp) == 1
    assert emp[0].value == "Acme Corp"
    assert emp[0].source == ExtractionSource.anchor
    # W-2 employer label synonyms were forwarded to the helper.
    assert "Employer" in captured["labels"]
    # Identifier sweep still ran alongside the anchored fields.
    assert len(_by_key(fields, "id.ein")) == 1


def test_no_lines_skips_anchored(monkeypatch):
    """With no OCR lines, the anchored path is skipped entirely (anchor_extract not called)."""
    import di.extract.deterministic.anchored_kv as akv

    def boom(*_a, **_k):
        raise AssertionError("anchor_extract should not be called without lines")

    monkeypatch.setattr(akv, "anchor_extract", boom)
    fields = _fields(VALID_SSN, doc_type="US_SSN_CARD", lines=None)
    assert len(_by_key(fields, "id.ssn")) == 1


def test_module_imports_without_optional_deps():
    """Importing the module must not require any heavy/optional dependency."""
    import importlib

    mod = importlib.import_module("di.extract.deterministic.us")
    assert hasattr(mod, "US_EXTRACTOR")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
