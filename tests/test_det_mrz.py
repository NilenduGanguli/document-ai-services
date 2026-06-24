"""Unit tests for the passport (ICAO 9303 TD3 MRZ) deterministic extractor."""
from __future__ import annotations

import pytest

from di.extract.base import ExtractionInput, get_extractor
from di.models import ExtractionSource, VerificationStatus

# Known-valid ICAO 9303 TD3 sample (UTO / ANNA MARIA ERIKSSON). All check digits valid.
VALID_LINE1 = "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<"
VALID_LINE2 = "L898902C36UTO7408122F1204159ZE184226B<<<<<10"
VALID_MRZ = f"{VALID_LINE1}\n{VALID_LINE2}"

# Same zone with the document-number check digit corrupted ('6' -> '5').
CORRUPT_LINE2 = "L898902C35UTO7408122F1204159ZE184226B<<<<<10"
CORRUPT_MRZ = f"{VALID_LINE1}\n{CORRUPT_LINE2}"

# Realistic OCR dump: header noise above the MRZ zone.
OCR_WITH_NOISE = (
    "REPUBLIC OF UTOPIA           PASSPORT\n"
    "Type: P   Code: UTO   Passport No: L898902C3\n"
    "Surname: ERIKSSON   Given names: ANNA MARIA\n"
    f"{VALID_LINE1}\n"
    f"{VALID_LINE2}\n"
)


def _by_key(fields):
    return {f.attribute_key: f for f in fields}


def _extractor():
    pytest.importorskip("mrz")
    ex = get_extractor("PASSPORT")
    assert ex is not None, "PASSPORT extractor must register itself at import"
    return ex


def test_registered_for_passport():
    # Importing the module registers the extractor for the PASSPORT doc_type.
    import di.extract.deterministic.mrz as mrz_mod

    assert "PASSPORT" in mrz_mod.PassportMrzExtractor.handles
    assert get_extractor("PASSPORT") is not None


def test_valid_mrz_extracts_passport_number_and_dob():
    ex = _extractor()
    fields = ex.extract(ExtractionInput(doc_type="PASSPORT", text=VALID_MRZ))
    by_key = _by_key(fields)

    pno = by_key["id.passport_number"]
    assert pno.value == "L898902C3"
    assert pno.source == ExtractionSource.mrz
    assert pno.checksum_ok is True
    assert pno.verification_status == VerificationStatus.checksum_verified
    assert pno.confidence > 0.9

    dob = by_key["identity.date_of_birth"]
    assert dob.value_date is not None
    assert dob.value_date.isoformat() == "1974-08-12"
    assert dob.checksum_ok is True

    # Remaining mapped attributes are present and consistent.
    assert by_key["identity.surname"].value == "ERIKSSON"
    assert by_key["identity.given_names"].value == "ANNA MARIA"
    assert by_key["identity.nationality"].value == "UTO"
    assert by_key["identity.sex"].value == "F"
    assert by_key["doc.expiry_date"].value_date.isoformat() == "2012-04-15"


def test_corrupted_check_digit_marks_checksum_false():
    ex = _extractor()
    fields = ex.extract(ExtractionInput(doc_type="PASSPORT", text=CORRUPT_MRZ))
    by_key = _by_key(fields)

    pno = by_key["id.passport_number"]
    assert pno.checksum_ok is False
    assert pno.verification_status == VerificationStatus.unverified
    assert pno.confidence < 0.9
    # The value is still surfaced for review even though validation failed.
    assert pno.value == "L898902C3"


def test_finds_mrz_inside_noisy_ocr():
    ex = _extractor()
    fields = ex.extract(ExtractionInput(doc_type="PASSPORT", text=OCR_WITH_NOISE))
    by_key = _by_key(fields)
    assert by_key["id.passport_number"].value == "L898902C3"
    assert by_key["id.passport_number"].checksum_ok is True


def test_no_mrz_returns_empty():
    ex = _extractor()
    fields = ex.extract(
        ExtractionInput(doc_type="PASSPORT", text="Just some prose with no MRZ zone here.")
    )
    assert fields == []


def test_positional_fallback_without_mrz_lib():
    # Exercise the deterministic fallback path directly (no dependence on the mrz lib).
    from di.extract.deterministic.mrz import PassportMrzExtractor

    ex = PassportMrzExtractor()
    fields = ex._fields_from_positions(VALID_LINE1, VALID_LINE2)
    by_key = _by_key(fields)

    pno = by_key["id.passport_number"]
    assert pno.value == "L898902C3"
    assert pno.checksum_ok is None  # no validation possible without the lib
    assert pno.verification_status == VerificationStatus.unverified
    assert pno.source == ExtractionSource.mrz
    assert by_key["identity.surname"].value == "ERIKSSON"
    assert by_key["identity.date_of_birth"].value_date.isoformat() == "1974-08-12"
