"""Unit tests for the Canada deterministic extractor.

Covers: SIN Luhn validity (valid vs invalid), Business Number structure (9-digit + BN15
program account), T4 employer/amount extraction, NOA amounts, registry wiring, and the lazy
sibling ``anchored_kv`` helper being honoured when present (monkeypatched).
"""
from __future__ import annotations

import pytest

from di.extract.base import ExtractionInput, get_extractor
from di.extract.deterministic import canada
from di.models import ExtractionSource, SensitivityBucket, VerificationStatus

# ``stdnum`` is a light, always-installed dep, but skip cleanly if a stripped env lacks it.
pytest.importorskip("stdnum.ca.sin")
pytest.importorskip("stdnum.ca.bn")

# Known-good fixtures (computed against stdnum's own checksums).
VALID_SIN = "130692544"        # passes Luhn
INVALID_SIN = "046454286"      # fails Luhn
VALID_BN9 = "100000009"        # 9-digit BN, valid Luhn
VALID_BN15 = "100000009RT0001"  # BN15 with a program-account suffix
INVALID_BN = "123456789"       # fails Luhn


def _ex() -> canada.CanadaExtractor:
    return canada.CanadaExtractor()


def test_registered() -> None:
    assert get_extractor("CA_SIN") is not None
    assert get_extractor("CA_BUSINESS_NUMBER") is not None
    assert canada.CanadaExtractor.handles == frozenset(
        {"CA_SIN", "CA_BUSINESS_NUMBER", "CA_T4", "CA_NOA", "CA_DRIVER_LICENSE"}
    )


def test_valid_sin_checksum_verified() -> None:
    inp = ExtractionInput(doc_type="CA_SIN", text=f"Social Insurance Number {VALID_SIN}")
    fields = _ex().extract(inp)
    assert len(fields) == 1
    f = fields[0]
    assert f.attribute_key == "id.sin"
    assert f.value == "130-692-544"  # stdnum-formatted
    assert f.checksum_ok is True
    assert f.verification_status == VerificationStatus.checksum_verified
    assert f.source == ExtractionSource.regex_sweep
    assert f.sensitivity == SensitivityBucket.critical


def test_invalid_sin_dropped() -> None:
    inp = ExtractionInput(doc_type="CA_SIN", text=f"SIN: {INVALID_SIN}")
    assert _ex().extract(inp) == []


def test_sin_accepts_grouped_format() -> None:
    inp = ExtractionInput(doc_type="CA_SIN", text="SIN 130-692-544 on file")
    fields = _ex().extract(inp)
    assert len(fields) == 1
    assert fields[0].value == "130-692-544"
    assert fields[0].raw_ocr == "130-692-544"


def test_valid_business_number_9_digit() -> None:
    inp = ExtractionInput(
        doc_type="CA_BUSINESS_NUMBER",
        text=f"Business Number (Numéro d'entreprise): {VALID_BN9}",
    )
    fields = _ex().extract(inp)
    assert len(fields) == 1
    f = fields[0]
    assert f.attribute_key == "id.business_number"
    assert f.value == VALID_BN9
    assert f.checksum_ok is True
    assert f.verification_status == VerificationStatus.checksum_verified
    assert f.sensitivity == SensitivityBucket.high


def test_valid_business_number_bn15_program_account() -> None:
    inp = ExtractionInput(doc_type="CA_BUSINESS_NUMBER", text=f"BN {VALID_BN15}")
    fields = _ex().extract(inp)
    assert len(fields) == 1
    assert fields[0].value == VALID_BN15
    assert fields[0].checksum_ok is True


def test_invalid_business_number_dropped() -> None:
    inp = ExtractionInput(doc_type="CA_BUSINESS_NUMBER", text=f"BN {INVALID_BN}")
    assert _ex().extract(inp) == []


def test_t4_employer_and_amount_fallback() -> None:
    text = (
        "T4 Statement of Remuneration Paid\n"
        "Employer's name: ACME Manufacturing Inc\n"
        "Box 14 Employment income: $54,321.00\n"
        f"Social Insurance Number: {VALID_SIN}\n"
    )
    fields = _ex().extract(ExtractionInput(doc_type="CA_T4", text=text))
    by_key = {f.attribute_key: f for f in fields}
    assert by_key["income.employer"].value == "ACME Manufacturing Inc"
    assert by_key["income.amount"].value_num == 54321.00
    # T4 surfaces a checksum-valid employee SIN.
    assert by_key["id.sin"].checksum_ok is True
    assert by_key["id.sin"].sensitivity == SensitivityBucket.critical


def test_noa_amounts_fallback() -> None:
    text = (
        "Notice of Assessment (Avis de cotisation) - CRA\n"
        "Total income: 88,000.00\n"
        "Mailing address: 123 Main St, Toronto ON M5V 1A1\n"
    )
    fields = _ex().extract(ExtractionInput(doc_type="CA_NOA", text=text))
    keys = {f.attribute_key for f in fields}
    assert "income.amount" in keys
    assert "address.mailing" in keys
    amt = next(f for f in fields if f.attribute_key == "income.amount")
    assert amt.value_num == 88000.00


def test_unknown_doc_type_returns_empty() -> None:
    inp = ExtractionInput(doc_type="US_W2", text="irrelevant")
    assert _ex().extract(inp) == []


def test_lazy_sibling_anchored_kv_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the sibling helper exists, it is preferred over the built-in fallback."""
    calls: list[tuple[str, ...]] = []

    def fake_helper(*, text, lines, anchors, lang):  # noqa: ANN001, ANN202
        calls.append(anchors)
        return [("Fake Employer Ltd", None)]

    fake_module = type("M", (), {"anchored_kv": staticmethod(fake_helper)})
    import sys

    monkeypatch.setitem(sys.modules, "di.extract.anchors", fake_module)

    text = "T4 Employer's name: Real Co\nEmployment income: $10.00\n"
    fields = _ex().extract(ExtractionInput(doc_type="CA_T4", text=text))
    employer = next(f for f in fields if f.attribute_key == "income.employer")
    assert employer.value == "Fake Employer Ltd"
    assert calls  # helper was actually invoked
