"""Deterministic passport extractor — ICAO 9303 TD3 Machine-Readable Zone.

Parses the two-line, 44-character-each TD3 MRZ found at the bottom of a passport's
data page and maps its fields onto the canonical attribute-key catalog. Validation
relies entirely on the ICAO 9303 check digits computed by the ``mrz`` library, so a
single corrupted character flips ``checksum_ok`` to ``False`` and downgrades the
verification status.

The ``mrz`` dependency is optional (the ``extract`` extra) and is imported lazily inside
:meth:`PassportMrzExtractor.extract`. When it is absent the extractor still imports and
registers; it simply finds the TD3 zone, reports the parsed values with
``checksum_ok=None`` / ``verification_status=unverified`` and a low confidence.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime

from di.extract.base import ExtractionInput, register
from di.models import (
    ExtractedField,
    ExtractionSource,
    SensitivityBucket,
    VerificationStatus,
)

# A single TD3 line: exactly 44 chars from the MRZ alphabet (A-Z, 0-9, filler '<').
_TD3_LINE = re.compile(r"[A-Z0-9<]{44}")
# Line 1 of a passport TD3 zone begins with the document type 'P' followed by a
# subtype letter or the filler, then the 3-letter issuing-state code.
_TD3_LINE1_PREFIX = re.compile(r"^P[A-Z<][A-Z<]{3}")

# Confidence levels for the three possible outcomes.
_CONF_VERIFIED = 0.99
_CONF_PARSED_NO_LIB = 0.50
_CONF_CHECKSUM_FAILED = 0.40


def _normalize(text: str) -> str:
    """Uppercase and replace common OCR confusables of the filler character.

    Passport MRZs use '<' as filler; some OCR engines emit it as a run of
    less-than-likes or stray spaces inside the zone. We only uppercase and map a
    couple of obvious filler look-alikes; we deliberately do NOT touch alphanumerics
    so the check digits stay meaningful.
    """
    return text.upper().replace("«", "<").replace("‹", "<")


def _find_td3(text: str) -> tuple[str, str] | None:
    """Locate the two 44-char TD3 lines in an OCR dump.

    Returns ``(line1, line2)`` or ``None`` if no plausible zone is found. We scan
    line-by-line for two consecutive 44-char MRZ-alphabet lines whose first line looks
    like a passport line-1 (``P`` + subtype + issuing state).
    """
    raw_lines = [_normalize(ln).strip() for ln in _normalize(text).splitlines()]
    candidates: list[str] = []
    for ln in raw_lines:
        # An OCR line may contain trailing/leading noise; pull the 44-char window out.
        match = _TD3_LINE.search(ln)
        if match is not None and len(ln.replace(" ", "")) >= 44:
            stripped = ln.replace(" ", "")
            window = _TD3_LINE.search(stripped)
            if window is not None:
                candidates.append(window.group(0))
        elif match is not None and len(ln) == 44:
            candidates.append(match.group(0))

    for i in range(len(candidates) - 1):
        line1, line2 = candidates[i], candidates[i + 1]
        if _TD3_LINE1_PREFIX.match(line1):
            return line1, line2
    return None


def _parse_mrz_date(yymmdd: str, *, is_birth: bool) -> date | None:
    """Parse a 6-digit YYMMDD MRZ date using ICAO 9303 century windowing.

    Birth dates resolve to the most recent matching past year; expiry/validity dates
    resolve forward. We use the current year's last two digits as the pivot for births
    so e.g. a DOB of ``050101`` reads as 2005 (not 1905) when plausible.
    """
    if not re.fullmatch(r"\d{6}", yymmdd):
        return None
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    current_yy = datetime.now(UTC).year % 100
    if is_birth:
        century = 1900 if yy > current_yy else 2000
    else:
        # Expiry/validity dates are at or after issuance; treat near values as 2000s.
        century = 2000 if yy <= (current_yy + 50) else 1900
    try:
        return date(century + yy, mm, dd)
    except ValueError:
        return None


def _clean_names(value: str) -> str:
    """Collapse MRZ filler ('<') into spaces and squeeze whitespace."""
    return re.sub(r"\s+", " ", value.replace("<", " ")).strip()


def _field(
    attribute_key: str,
    value: str | None,
    *,
    checksum_ok: bool | None,
    confidence: float,
    value_date: date | None = None,
    raw_ocr: str | None = None,
    sensitivity: SensitivityBucket = SensitivityBucket.high,
) -> ExtractedField:
    status = (
        VerificationStatus.checksum_verified
        if checksum_ok
        else VerificationStatus.unverified
    )
    return ExtractedField(
        attribute_key=attribute_key,
        value=value or None,
        value_date=value_date,
        raw_ocr=raw_ocr,
        source=ExtractionSource.mrz,
        checksum_ok=checksum_ok,
        verification_status=status,
        confidence=confidence,
        sensitivity=sensitivity,
    )


class PassportMrzExtractor:
    """ICAO 9303 TD3 passport MRZ extractor (implements ``DeterministicExtractor``)."""

    handles: frozenset[str] = frozenset({"PASSPORT"})

    def extract(self, inp: ExtractionInput) -> list[ExtractedField]:
        zone = _find_td3(inp.text)
        if zone is None:
            return []
        line1, line2 = zone
        mrz_block = f"{line1}\n{line2}"

        checker = self._check(mrz_block)
        if checker is not None:
            return self._fields_from_checker(checker)
        # mrz library unavailable -> deterministic positional fallback.
        return self._fields_from_positions(line1, line2)

    # -- mrz-library path -------------------------------------------------------
    @staticmethod
    def _check(mrz_block: str) -> object | None:
        """Run the TD3 checker; return ``None`` if the optional ``mrz`` dep is absent."""
        try:
            from mrz.checker.td3 import TD3CodeChecker
        except ImportError:
            return None
        try:
            return TD3CodeChecker(mrz_block)
        except Exception:
            # mrz raises on malformed input (wrong length, bad chars). Treat as
            # "found a zone but could not validate" -> positional fallback decides.
            return None

    def _fields_from_checker(self, checker: object) -> list[ExtractedField]:
        valid = bool(checker)
        f = checker.fields()  # type: ignore[attr-defined]
        conf = _CONF_VERIFIED if valid else _CONF_CHECKSUM_FAILED

        surname = _clean_names(f.surname)
        given = _clean_names(f.name)
        passport_no = f.document_number.replace("<", "").strip()
        nationality = self._normalize_country(f.nationality)
        dob = _parse_mrz_date(f.birth_date, is_birth=True)
        sex = self._normalize_sex(f.sex)
        expiry = _parse_mrz_date(f.expiry_date, is_birth=False)

        fields: list[ExtractedField] = [
            _field("identity.surname", surname, checksum_ok=valid, confidence=conf,
                   raw_ocr=f.surname),
            _field("identity.given_names", given, checksum_ok=valid, confidence=conf,
                   raw_ocr=f.name),
            _field("id.passport_number", passport_no, checksum_ok=valid, confidence=conf,
                   raw_ocr=f.document_number),
            _field("identity.nationality", nationality, checksum_ok=valid, confidence=conf,
                   raw_ocr=f.nationality, sensitivity=SensitivityBucket.medium),
            _field("identity.date_of_birth", f.birth_date if dob is None else dob.isoformat(),
                   checksum_ok=valid, confidence=conf, value_date=dob, raw_ocr=f.birth_date),
            _field("identity.sex", sex, checksum_ok=valid, confidence=conf, raw_ocr=f.sex,
                   sensitivity=SensitivityBucket.medium),
            _field("doc.expiry_date", f.expiry_date if expiry is None else expiry.isoformat(),
                   checksum_ok=valid, confidence=conf, value_date=expiry, raw_ocr=f.expiry_date,
                   sensitivity=SensitivityBucket.low),
        ]
        return fields

    # -- positional fallback (no mrz library) -----------------------------------
    def _fields_from_positions(self, line1: str, line2: str) -> list[ExtractedField]:
        """Slice the TD3 zone by fixed offsets (ICAO 9303). No checksum validation."""
        conf = _CONF_PARSED_NO_LIB
        identifier = line1[5:44]
        # Names are 'SURNAME<<GIVEN<NAMES'.
        parts = identifier.split("<<", 1)
        surname = _clean_names(parts[0])
        given = _clean_names(parts[1]) if len(parts) > 1 else ""

        passport_raw = line2[0:9]
        passport_no = passport_raw.replace("<", "").strip()
        nationality_raw = line2[10:13]
        birth_raw = line2[13:19]
        sex_raw = line2[20]
        expiry_raw = line2[21:27]

        dob = _parse_mrz_date(birth_raw, is_birth=True)
        expiry = _parse_mrz_date(expiry_raw, is_birth=False)

        return [
            _field("identity.surname", surname, checksum_ok=None, confidence=conf,
                   raw_ocr=parts[0]),
            _field("identity.given_names", given, checksum_ok=None, confidence=conf,
                   raw_ocr=(parts[1] if len(parts) > 1 else None)),
            _field("id.passport_number", passport_no, checksum_ok=None, confidence=conf,
                   raw_ocr=passport_raw),
            _field("identity.nationality", self._normalize_country(nationality_raw),
                   checksum_ok=None, confidence=conf, raw_ocr=nationality_raw,
                   sensitivity=SensitivityBucket.medium),
            _field("identity.date_of_birth", birth_raw if dob is None else dob.isoformat(),
                   checksum_ok=None, confidence=conf, value_date=dob, raw_ocr=birth_raw),
            _field("identity.sex", self._normalize_sex(sex_raw), checksum_ok=None,
                   confidence=conf, raw_ocr=sex_raw, sensitivity=SensitivityBucket.medium),
            _field("doc.expiry_date", expiry_raw if expiry is None else expiry.isoformat(),
                   checksum_ok=None, confidence=conf, value_date=expiry, raw_ocr=expiry_raw,
                   sensitivity=SensitivityBucket.low),
        ]

    # -- normalizers ------------------------------------------------------------
    @staticmethod
    def _normalize_sex(raw: str) -> str | None:
        token = raw.strip().upper().replace("<", "")
        mapping = {"M": "M", "F": "F"}
        return mapping.get(token)  # '<' / 'X' / unspecified -> None

    @staticmethod
    def _normalize_country(raw: str) -> str:
        """Return the 3-letter MRZ country code, validated against ISO 3166 if available.

        ``pycountry`` is a light installed dep but kept lazy so the module never fails to
        import for any reason. The MRZ code is returned verbatim if no ISO match is found
        (MRZ permits a few non-ISO codes such as 'UTO' / 'D' / 'GBR' variants).
        """
        code = raw.strip().upper().replace("<", "")
        if not code:
            return code
        try:
            import pycountry
        except ImportError:
            return code
        try:
            match = pycountry.countries.get(alpha_3=code)
        except (KeyError, LookupError):
            match = None
        return match.alpha_3 if match is not None else code


# Register at import so the pipeline can dispatch PASSPORT -> this extractor.
register(PassportMrzExtractor())
