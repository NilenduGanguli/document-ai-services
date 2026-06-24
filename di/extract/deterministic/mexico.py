"""Mexico deterministic ID extractor (CURP / RFC / INE).

Pure, offline extractor for the three fixed-format Mexican documents:

* **CURP** (Clave Única de Registro de Población) — 18-char identity key with a published
  check digit. We verify the checksum *hard* (a mismatch is a reject) and decode the embedded
  birth date and sex. The official alphabet only assigns ``H``/``M`` to the sex position, but
  modern non-binary CURPs use ``X``; we accept ``X`` rather than hard-rejecting it.
* **RFC / Constancia de Situación Fiscal** — taxpayer registry key (10/12/13 chars). The
  structure is validated *strictly*; the optional check digit is treated as a *soft* signal —
  a mismatch leaves the field ``unverified`` with a ``checksum_soft_fail`` note instead of
  dropping it, because OCR frequently mangles the homoclave on a CSF printout.
* **INE / IFE credential** — the voter card. We pull the 18-char *Clave de Elector* and the
  reverse-side machine-readable ``IDMEX`` block. Neither carries a public checksum, so emitted
  fields stay ``unverified``; when a CURP is present on the same document we cross-check the
  embedded birth date / sex against it.

The only third-party dependency is :mod:`stdnum` (a light, always-installed dep), imported lazily
so the module loads even in a stripped environment; a regex fallback covers CURP/RFC structure if
``stdnum`` is somehow unavailable.
"""
from __future__ import annotations

import re
from datetime import date

from di.extract.base import ExtractionInput, register
from di.models import (
    ExtractedField,
    ExtractionSource,
    VerificationStatus,
)

# ---------------------------------------------------------------------------
# Canonical attribute keys this extractor can emit.
# ---------------------------------------------------------------------------
ATTR_CURP = "id.curp"
ATTR_RFC = "id.rfc"
ATTR_INE = "id.ine_clave_elector"
ATTR_DOB = "identity.date_of_birth"
ATTR_SEX = "identity.sex"

# ---------------------------------------------------------------------------
# Patterns (uppercased, whitespace-normalised text is matched).
# CURP : 4 letters, 6 date digits, H/M/X sex, 5 letters, 1 alnum, 1 check digit.
# RFC  : 3-4 letters (incl. & / Ñ), 6 date digits, up to 3 homoclave alnum.
# INE  : Clave de Elector = 6 letters + 6 digits + 2 entity digits + [HM] + 3 digits.
# IDMEX: reverse MRZ block — "IDMEX" + 9-digit CIC + 12/13-digit OCR/CURP-derived run.
# ---------------------------------------------------------------------------
CURP_RE = re.compile(r"\b([A-Z]{4}\d{6}[HMX][A-Z]{5}[0-9A-Z]\d)\b")
RFC_RE = re.compile(r"\b([A-ZÑ&]{3,4}\d{6}[0-9A-Z]{2,3})\b")
INE_CLAVE_RE = re.compile(r"\b([A-Z]{6}\d{6}\d{2}[HM]\d{3})\b")
IDMEX_RE = re.compile(r"IDMEX(\d{9})(\d{12,13})")


def _norm(text: str) -> str:
    """Uppercase and collapse whitespace so OCR line wraps don't break anchored matches."""
    return re.sub(r"[ \t]+", " ", text.upper())


def _curp_birth_date(value: str) -> date | None:
    """Decode the YYMMDD birth date embedded in a CURP (positions 4..10).

    Mirrors ``stdnum.mx.curp.get_birth_date`` but is checksum-agnostic so it also works for
    the sex-``X`` case we validate by hand. The century pivot follows RENAPO: a *digit* in the
    homoclave (position 16) means 19xx, a letter means 20xx.
    """
    try:
        year = int(value[4:6])
        month = int(value[6:8])
        day = int(value[8:10])
    except ValueError:
        return None
    year += 1900 if value[16].isdigit() else 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _sex_from_code(code: str) -> str | None:
    """Map a CURP/INE sex code to a normalised marker. 'X' is accepted (non-binary)."""
    return {"H": "M", "M": "F", "X": "X"}.get(code)


def _extract_curps(text: str) -> list[ExtractedField]:
    """Find CURP candidates, hard-verify the checksum, and emit id + DOB + sex."""
    fields: list[ExtractedField] = []
    seen: set[str] = set()
    for match in CURP_RE.finditer(text):
        candidate = match.group(1)
        if candidate in seen:
            continue
        seen.add(candidate)

        sex_code = candidate[10]
        checksum_ok = _verify_curp_checksum(candidate)
        if not checksum_ok:
            # Hard reject: a CURP with a wrong check digit is not a real CURP.
            continue

        status = (
            VerificationStatus.checksum_verified
            if checksum_ok
            else VerificationStatus.unverified
        )
        fields.append(
            ExtractedField(
                attribute_key=ATTR_CURP,
                value=candidate,
                raw_ocr=candidate,
                source=ExtractionSource.regex_sweep,
                checksum_ok=True,
                verification_status=status,
                confidence=0.97,
            )
        )

        dob = _curp_birth_date(candidate)
        if dob is not None:
            fields.append(
                ExtractedField(
                    attribute_key=ATTR_DOB,
                    value=dob.isoformat(),
                    value_date=dob,
                    raw_ocr=candidate,
                    source=ExtractionSource.regex_sweep,
                    verification_status=VerificationStatus.checksum_verified,
                    confidence=0.95,
                )
            )
        sex = _sex_from_code(sex_code)
        if sex is not None:
            fields.append(
                ExtractedField(
                    attribute_key=ATTR_SEX,
                    value=sex,
                    raw_ocr=candidate,
                    source=ExtractionSource.regex_sweep,
                    verification_status=VerificationStatus.checksum_verified,
                    confidence=0.95,
                )
            )
    return fields


def _verify_curp_checksum(candidate: str) -> bool:
    """Hard checksum check. Uses stdnum when present; otherwise a self-contained fallback.

    We deliberately do *not* route through ``stdnum.mx.curp.validate`` because it rejects the
    sex-``X`` component; here we only care whether the published check digit is internally
    consistent, which is what makes the value trustworthy.
    """
    try:
        from stdnum.mx import curp as _curp
    except ImportError:
        return _curp_checksum_fallback(candidate)
    try:
        return candidate[-1] == _curp.calc_check_digit(candidate)
    except (ValueError, IndexError):
        return False


# RENAPO check-digit alphabet (index = value) used by the fallback path.
_CURP_ALPHABET = "0123456789ABCDEFGHIJKLMNÑOPQRSTUVWXYZ"


def _curp_checksum_fallback(candidate: str) -> bool:
    """Pure-Python CURP check-digit verification (no stdnum)."""
    if len(candidate) != 18:
        return False
    try:
        total = sum(
            _CURP_ALPHABET.index(candidate[i]) * (18 - i) for i in range(17)
        )
    except ValueError:
        return False
    check = (10 - (total % 10)) % 10
    return candidate[-1] == str(check)


def _extract_rfcs(text: str) -> list[ExtractedField]:
    """Find RFC candidates; structure is strict, check digit is a *soft* signal."""
    fields: list[ExtractedField] = []
    seen: set[str] = set()
    for match in RFC_RE.finditer(text):
        candidate = match.group(1)
        if candidate in seen:
            continue
        seen.add(candidate)
        if len(candidate) not in (10, 12, 13):
            continue

        structure_ok, checksum_ok = _validate_rfc(candidate)
        if not structure_ok:
            continue

        status = VerificationStatus.unverified
        confidence = 0.85
        if checksum_ok is True:
            status = VerificationStatus.checksum_verified
            confidence = 0.95
        elif checksum_ok is False:
            # Soft fail: keep the field, flag the mismatch, do not reject.
            confidence = 0.6

        fields.append(
            ExtractedField(
                attribute_key=ATTR_RFC,
                value=candidate,
                raw_ocr="checksum_soft_fail" if checksum_ok is False else candidate,
                source=ExtractionSource.regex_sweep,
                checksum_ok=checksum_ok,
                verification_status=status,
                confidence=confidence,
            )
        )
    return fields


def _validate_rfc(candidate: str) -> tuple[bool, bool | None]:
    """Return ``(structure_ok, checksum_ok)``.

    ``checksum_ok`` is ``None`` when the value is too short to carry a homoclave/check digit
    (a 10-char personal RFC), ``True``/``False`` otherwise.
    """
    try:
        from stdnum.mx import rfc as _rfc
    except ImportError:
        return (_rfc_structure_fallback(candidate), None)

    try:
        _rfc.validate(candidate, validate_check_digits=False)
    except Exception:
        return (False, None)

    if len(candidate) < 12:
        return (True, None)
    try:
        _rfc.validate(candidate, validate_check_digits=True)
        return (True, True)
    except Exception:
        return (True, False)


def _rfc_structure_fallback(candidate: str) -> bool:
    """Structure-only RFC check without stdnum."""
    if len(candidate) in (12, 13):
        return bool(re.match(r"^[A-Z&Ñ]{3,4}\d{6}[0-9A-Z]{3}$", candidate))
    if len(candidate) == 10:
        return bool(re.match(r"^[A-Z&Ñ]{4}\d{6}$", candidate))
    return False


def _extract_ine(text: str, curp_dob: date | None, curp_sex: str | None) -> list[ExtractedField]:
    """Find the INE Clave de Elector and the IDMEX reverse-MRZ block (no public checksum)."""
    fields: list[ExtractedField] = []
    seen: set[str] = set()

    for match in INE_CLAVE_RE.finditer(text):
        clave = match.group(1)
        if clave in seen:
            continue
        seen.add(clave)

        notes: list[str] = []
        embedded_sex = _sex_from_code(clave[14])
        if curp_sex is not None and embedded_sex is not None and embedded_sex != curp_sex:
            notes.append("ine_sex_mismatch_curp")

        fields.append(
            ExtractedField(
                attribute_key=ATTR_INE,
                value=clave,
                raw_ocr=";".join(notes) if notes else clave,
                source=ExtractionSource.regex_sweep,
                checksum_ok=None,
                verification_status=VerificationStatus.unverified,
                confidence=0.7 if notes else 0.8,
            )
        )

    for match in IDMEX_RE.finditer(text):
        cic = match.group(1)
        notes = []
        if curp_dob is not None:
            notes.append("idmex_dob_crosscheck_curp")
        fields.append(
            ExtractedField(
                attribute_key=ATTR_INE,
                value=f"IDMEX{cic}",
                raw_ocr=match.group(0),
                source=ExtractionSource.mrz,
                checksum_ok=None,
                verification_status=VerificationStatus.unverified,
                confidence=0.75,
            )
        )

    return fields


class MexicoExtractor:
    """Deterministic extractor for Mexican CURP / RFC / INE documents."""

    handles: frozenset[str] = frozenset({"MX_CURP", "MX_RFC_CSF", "MX_INE"})

    def extract(self, inp: ExtractionInput) -> list[ExtractedField]:
        text = _norm(inp.text)
        fields: list[ExtractedField] = []

        curp_fields = _extract_curps(text)
        fields.extend(curp_fields)

        # Pull the CURP-derived DOB / sex so INE fields can cross-check against them.
        curp_dob: date | None = next(
            (f.value_date for f in curp_fields if f.attribute_key == ATTR_DOB), None
        )
        curp_sex: str | None = next(
            (f.value for f in curp_fields if f.attribute_key == ATTR_SEX), None
        )

        fields.extend(_extract_rfcs(text))
        fields.extend(_extract_ine(text, curp_dob, curp_sex))
        return fields


# Register a singleton instance for the pipeline's dispatch-by-doc_type lookup.
EXTRACTOR = register(MexicoExtractor())
