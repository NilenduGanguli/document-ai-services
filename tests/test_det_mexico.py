"""Unit tests for the Mexico deterministic extractor (CURP / RFC / INE).

Pure-logic: stdnum is a light, always-installed dependency, but the fallback paths are also
exercised directly so the module's behaviour holds even without it.
"""
from __future__ import annotations

from datetime import date

from di.extract.base import ExtractionInput, get_extractor
from di.extract.deterministic import mexico
from di.models import ExtractionSource, VerificationStatus

# --- Test fixtures (check digits computed against stdnum at authoring time) ---
VALID_CURP = "BOXW310820HNERMN07"          # sex H -> M, DOB 1931-08-20, valid checksum
SEX_X_CURP = "BOXW310820XNERMN01"          # sex X, valid checksum, stdnum rejects on gender
GOOD_RFC = "VACE860307JN1"                 # 13-char personal RFC, valid check digit
BAD_CHECK_RFC = "VACE860307JNA"            # same structure, wrong check digit (soft fail)
INE_CLAVE = "GMRRJL85031509H123"           # 6 letters + 6 digits + 2 entity + H + 3 digits


def _fields_by_key(fields, key):
    return [f for f in fields if f.attribute_key == key]


def test_extractor_registered():
    for code in ("MX_CURP", "MX_RFC_CSF", "MX_INE"):
        ext = get_extractor(code)
        assert ext is not None, code
        assert isinstance(ext, mexico.MexicoExtractor)
    assert mexico.MexicoExtractor.handles == frozenset({"MX_CURP", "MX_RFC_CSF", "MX_INE"})


def test_valid_curp_yields_id_dob_sex():
    ext = mexico.MexicoExtractor()
    out = ext.extract(ExtractionInput(doc_type="MX_CURP", text=f"CURP: {VALID_CURP}"))

    curp_fields = _fields_by_key(out, mexico.ATTR_CURP)
    assert len(curp_fields) == 1
    cf = curp_fields[0]
    assert cf.value == VALID_CURP
    assert cf.checksum_ok is True
    assert cf.verification_status == VerificationStatus.checksum_verified
    assert cf.source == ExtractionSource.regex_sweep

    dob_fields = _fields_by_key(out, mexico.ATTR_DOB)
    assert len(dob_fields) == 1
    assert dob_fields[0].value_date == date(1931, 8, 20)

    sex_fields = _fields_by_key(out, mexico.ATTR_SEX)
    assert len(sex_fields) == 1
    assert sex_fields[0].value == "M"


def test_sex_x_curp_accepted_not_rejected():
    """A non-binary 'X' CURP with a valid check digit must validate and emit sex='X'."""
    ext = mexico.MexicoExtractor()
    out = ext.extract(ExtractionInput(doc_type="MX_CURP", text=f"CURP {SEX_X_CURP}"))

    curp_fields = _fields_by_key(out, mexico.ATTR_CURP)
    assert len(curp_fields) == 1, "sex-X CURP must not be hard-rejected"
    assert curp_fields[0].verification_status == VerificationStatus.checksum_verified

    sex_fields = _fields_by_key(out, mexico.ATTR_SEX)
    assert len(sex_fields) == 1
    assert sex_fields[0].value == "X"


def test_curp_bad_checksum_hard_rejected():
    bad = VALID_CURP[:-1] + ("0" if VALID_CURP[-1] != "0" else "1")
    ext = mexico.MexicoExtractor()
    out = ext.extract(ExtractionInput(doc_type="MX_CURP", text=f"CURP {bad}"))
    assert _fields_by_key(out, mexico.ATTR_CURP) == []


def test_good_rfc_checksum_verified():
    ext = mexico.MexicoExtractor()
    out = ext.extract(ExtractionInput(doc_type="MX_RFC_CSF", text=f"RFC: {GOOD_RFC}"))
    rfc_fields = _fields_by_key(out, mexico.ATTR_RFC)
    assert len(rfc_fields) == 1
    assert rfc_fields[0].value == GOOD_RFC
    assert rfc_fields[0].checksum_ok is True
    assert rfc_fields[0].verification_status == VerificationStatus.checksum_verified


def test_rfc_bad_check_digit_kept_soft_not_rejected():
    ext = mexico.MexicoExtractor()
    out = ext.extract(ExtractionInput(doc_type="MX_RFC_CSF", text=f"RFC {BAD_CHECK_RFC}"))
    rfc_fields = _fields_by_key(out, mexico.ATTR_RFC)
    assert len(rfc_fields) == 1, "bad-check-digit RFC must be kept (soft), not rejected"
    rf = rfc_fields[0]
    assert rf.value == BAD_CHECK_RFC
    assert rf.checksum_ok is False
    assert rf.verification_status == VerificationStatus.unverified
    assert rf.raw_ocr == "checksum_soft_fail"


def test_ine_clave_elector_matches():
    ext = mexico.MexicoExtractor()
    text = f"INSTITUTO NACIONAL ELECTORAL\nCLAVE DE ELECTOR {INE_CLAVE}"
    out = ext.extract(ExtractionInput(doc_type="MX_INE", text=text))
    ine_fields = _fields_by_key(out, mexico.ATTR_INE)
    assert any(f.value == INE_CLAVE for f in ine_fields)
    clave_field = next(f for f in ine_fields if f.value == INE_CLAVE)
    assert clave_field.verification_status == VerificationStatus.unverified
    assert clave_field.checksum_ok is None


def test_ine_idmex_mrz_matches():
    ext = mexico.MexicoExtractor()
    mrz = "IDMEX1234567890987654321098"  # IDMEX + 9-digit CIC + 13-digit OCR
    out = ext.extract(ExtractionInput(doc_type="MX_INE", text=mrz))
    ine_fields = _fields_by_key(out, mexico.ATTR_INE)
    assert any(f.source == ExtractionSource.mrz for f in ine_fields)


def test_ine_sex_crosscheck_with_curp_flags_mismatch():
    # INE clave encodes sex 'M' (-> F); CURP encodes 'H' (-> M). Expect a mismatch note.
    ext = mexico.MexicoExtractor()
    clave_female = "GMRRJL85031509M123"  # position 14 = M -> F
    text = f"CURP {VALID_CURP}\nCLAVE DE ELECTOR {clave_female}"
    out = ext.extract(ExtractionInput(doc_type="MX_INE", text=text))
    clave_field = next(
        f for f in out if f.attribute_key == mexico.ATTR_INE and f.value == clave_female
    )
    assert "ine_sex_mismatch_curp" in (clave_field.raw_ocr or "")


def test_curp_checksum_fallback_pure_python():
    """The no-stdnum fallback must agree with stdnum on known-valid/invalid CURPs."""
    assert mexico._curp_checksum_fallback(VALID_CURP) is True
    assert mexico._curp_checksum_fallback(SEX_X_CURP) is True
    bad = VALID_CURP[:-1] + ("0" if VALID_CURP[-1] != "0" else "1")
    assert mexico._curp_checksum_fallback(bad) is False
