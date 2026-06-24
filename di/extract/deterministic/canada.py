"""Canada deterministic extractor.

Pure, offline extraction of Canadian government identifiers and the structured fields of
common Canadian tax/income documents:

* ``CA_SIN``              — Social Insurance Number (validated via Luhn / ``stdnum.ca.sin``)
* ``CA_BUSINESS_NUMBER``  — CRA Business Number / BN15 (validated via ``stdnum.ca.bn``)
* ``CA_T4``               — Statement of Remuneration Paid (employer + amounts + SIN)
* ``CA_NOA``              — Notice of Assessment (amounts + mailing address)
* ``CA_DRIVER_LICENSE``   — Provincial driver's licence (name + DOB best-effort)

No database, no network. ``stdnum`` is a light, always-installed dependency and validates the
checksummed identifiers (so ``checksum_ok`` / ``checksum_verified`` is authoritative). The
anchored key/value helper (``di.extract.anchors.anchored_kv``) is a sibling Phase-1 module; it is
imported **lazily** and, when absent, this module falls back to a small built-in line/regex sweep
so it always imports and produces sensible deterministic output.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from di.extract.base import ExtractionInput, register
from di.models import (
    ExtractedField,
    ExtractionSource,
    SensitivityBucket,
    VerificationStatus,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from di.models import OcrLine

# ---------------------------------------------------------------------------
# Regexes (informational hints live in the ontology; the real patterns are here)
# ---------------------------------------------------------------------------
# SIN: 9 digits, optionally grouped 3-3-3 with space or hyphen.
_SIN_RE = re.compile(r"\b(\d{3}[ \-]?\d{3}[ \-]?\d{3})\b")
# BN / BN15: 9 digits, optional program-account suffix (RC/RM/RP/RT + 4 digits).
_BN_RE = re.compile(r"\b(\d{9}\s?(?:RC|RM|RP|RT)\s?\d{4}|\d{9})\b")
# Currency amounts, e.g. "$54,321.00", "12345.67", "1,234".
_AMOUNT_RE = re.compile(r"(?<![\w.])(?:CAD\s*)?\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)")

# Date-ish for driver licences / NOA (best-effort, fed to dateparser lazily).
_DATE_HINT_RE = re.compile(
    r"\b(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}|"
    r"[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Z][a-z]+\.?\s+\d{4})\b"
)

# Anchors used by the (fallback) line sweep to find labelled values.
_EMPLOYER_ANCHORS_EN = ("employer's name", "employer name", "employer", "nom de l'employeur")
_INCOME_ANCHORS_EN = (
    "employment income",
    "box 14",
    "total income",
    "net income",
    "taxable income",
    "revenu d'emploi",
    "revenu total",
)
_NOA_AMOUNT_ANCHORS = (
    "total income",
    "net income",
    "taxable income",
    "balance",
    "refund",
    "amount owing",
    "revenu total",
    "solde",
)
_NAME_ANCHORS_EN = (
    "name",
    "surname",
    "given name",
    "last name",
    "first name",
    "nom",
    "prénom",
)
_ADDRESS_ANCHORS_EN = ("mailing address", "address", "adresse")


def _clean_amount(raw: str) -> float | None:
    """Parse a currency-ish token into a float, or ``None`` if it is not a number."""
    token = raw.replace("CAD", "").replace("$", "").replace(",", "").strip()
    if not token:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def _anchored_kv(
    text: str,
    lines: list[OcrLine],
    anchors: tuple[str, ...],
    lang: str,
) -> list[tuple[str, OcrLine | None]]:
    """Find values sitting next to ``anchors``.

    Tries the sibling ``di.extract.anchors.anchored_kv`` helper first (imported lazily so this
    module never hard-depends on a concurrently-authored sibling). Falls back to a built-in
    line/text sweep when that helper is unavailable.
    """
    try:
        from di.extract.anchors import anchored_kv as _helper  # type: ignore[attr-defined]
    except ImportError:
        _helper = None

    if _helper is not None:
        try:
            return list(_helper(text=text, lines=lines, anchors=anchors, lang=lang))
        except TypeError:
            # Helper signature differs; degrade to fallback rather than crash.
            pass

    return _fallback_kv(text, lines, anchors)


def _fallback_kv(
    text: str,
    lines: list[OcrLine],
    anchors: tuple[str, ...],
) -> list[tuple[str, OcrLine | None]]:
    """Deterministic anchor sweep: return (value_after_anchor, source_line) pairs."""
    out: list[tuple[str, OcrLine | None]] = []
    lowered_anchors = tuple(a.lower() for a in anchors)

    def _scan(haystack: str, src: OcrLine | None) -> None:
        low = haystack.lower()
        for anchor in lowered_anchors:
            idx = low.find(anchor)
            if idx == -1:
                continue
            tail = haystack[idx + len(anchor) :].lstrip(" \t:;.-=")
            if tail:
                # Stop at a second anchor or a line break if present.
                value = tail.splitlines()[0].strip()
                if value:
                    out.append((value, src))

    if lines:
        for ln in lines:
            _scan(ln.text, ln)
    else:
        for raw_line in text.splitlines():
            _scan(raw_line, None)
    return out


def _make_field(
    *,
    attribute_key: str,
    value: str | None = None,
    value_num: float | None = None,
    raw_ocr: str | None = None,
    source: ExtractionSource = ExtractionSource.anchor,
    checksum_ok: bool | None = None,
    verification_status: VerificationStatus = VerificationStatus.unverified,
    confidence: float = 0.0,
    sensitivity: SensitivityBucket = SensitivityBucket.medium,
) -> ExtractedField:
    return ExtractedField(
        attribute_key=attribute_key,
        value=value,
        value_num=value_num,
        raw_ocr=raw_ocr,
        source=source,
        checksum_ok=checksum_ok,
        verification_status=verification_status,
        confidence=confidence,
        sensitivity=sensitivity,
    )


class CanadaExtractor:
    """Deterministic extractor for Canadian identifiers and tax/income documents."""

    handles: frozenset[str] = frozenset(
        {
            "CA_SIN",
            "CA_BUSINESS_NUMBER",
            "CA_T4",
            "CA_NOA",
            "CA_DRIVER_LICENSE",
        }
    )

    def extract(self, inp: ExtractionInput) -> list[ExtractedField]:
        dispatch: dict[str, Callable[[ExtractionInput], list[ExtractedField]]] = {
            "CA_SIN": self._extract_sin,
            "CA_BUSINESS_NUMBER": self._extract_bn,
            "CA_T4": self._extract_t4,
            "CA_NOA": self._extract_noa,
            "CA_DRIVER_LICENSE": self._extract_driver_license,
        }
        handler = dispatch.get(inp.doc_type)
        if handler is None:
            return []
        return handler(inp)

    # -- Identifiers --------------------------------------------------------
    def _extract_sin(self, inp: ExtractionInput) -> list[ExtractedField]:
        return self._sin_fields(inp.text)

    def _sin_fields(self, text: str) -> list[ExtractedField]:
        from stdnum.ca import sin as ca_sin

        fields: list[ExtractedField] = []
        seen: set[str] = set()
        for match in _SIN_RE.finditer(text):
            raw = match.group(1)
            compact = ca_sin.compact(raw)
            if compact in seen:
                continue
            ok = ca_sin.is_valid(compact)
            # An invalid 9-digit run is very likely not a SIN at all; emit only valid ones
            # for the SIN doc-type but keep raw_ocr for provenance.
            if not ok:
                continue
            seen.add(compact)
            formatted = ca_sin.format(compact)
            fields.append(
                _make_field(
                    attribute_key="id.sin",
                    value=formatted,
                    raw_ocr=raw,
                    source=ExtractionSource.regex_sweep,
                    checksum_ok=True,
                    verification_status=VerificationStatus.checksum_verified,
                    confidence=0.95,
                    sensitivity=SensitivityBucket.critical,
                )
            )
        return fields

    def _extract_bn(self, inp: ExtractionInput) -> list[ExtractedField]:
        return self._bn_fields(inp.text)

    def _bn_fields(self, text: str) -> list[ExtractedField]:
        from stdnum.ca import bn as ca_bn

        fields: list[ExtractedField] = []
        seen: set[str] = set()
        for match in _BN_RE.finditer(text):
            raw = match.group(1)
            compact = ca_bn.compact(raw)
            if compact in seen:
                continue
            ok = ca_bn.is_valid(compact)
            if not ok:
                continue
            seen.add(compact)
            fields.append(
                _make_field(
                    attribute_key="id.business_number",
                    value=compact,
                    raw_ocr=raw,
                    source=ExtractionSource.regex_sweep,
                    checksum_ok=True,
                    verification_status=VerificationStatus.checksum_verified,
                    confidence=0.95,
                    sensitivity=SensitivityBucket.high,
                )
            )
        return fields

    # -- Tax / income documents --------------------------------------------
    def _extract_t4(self, inp: ExtractionInput) -> list[ExtractedField]:
        fields: list[ExtractedField] = []
        fields.extend(self._employer(inp))
        fields.extend(self._income_amounts(inp, _INCOME_ANCHORS_EN))
        # T4 carries the employee SIN — surface any checksum-valid one.
        fields.extend(self._sin_fields(inp.text))
        return fields

    def _extract_noa(self, inp: ExtractionInput) -> list[ExtractedField]:
        fields: list[ExtractedField] = []
        fields.extend(self._income_amounts(inp, _NOA_AMOUNT_ANCHORS))
        fields.extend(self._mailing_address(inp))
        return fields

    def _extract_driver_license(self, inp: ExtractionInput) -> list[ExtractedField]:
        fields: list[ExtractedField] = []
        for value, src in _anchored_kv(inp.text, inp.lines, _NAME_ANCHORS_EN, inp.lang):
            cleaned = value.strip()
            if cleaned and not cleaned.isdigit():
                fields.append(
                    _make_field(
                        attribute_key="identity.full_name",
                        value=cleaned,
                        raw_ocr=src.text if src is not None else cleaned,
                        confidence=0.5,
                        sensitivity=SensitivityBucket.medium,
                    )
                )
                break
        dob = self._first_date(inp.text)
        if dob is not None:
            fields.append(
                _make_field(
                    attribute_key="identity.date_of_birth",
                    value=dob,
                    raw_ocr=dob,
                    confidence=0.4,
                    sensitivity=SensitivityBucket.medium,
                )
            )
        return fields

    # -- Shared field helpers ----------------------------------------------
    def _employer(self, inp: ExtractionInput) -> list[ExtractedField]:
        for value, src in _anchored_kv(inp.text, inp.lines, _EMPLOYER_ANCHORS_EN, inp.lang):
            cleaned = value.strip()
            if cleaned and _clean_amount(cleaned) is None:
                return [
                    _make_field(
                        attribute_key="income.employer",
                        value=cleaned,
                        raw_ocr=src.text if src is not None else cleaned,
                        confidence=0.5,
                        sensitivity=SensitivityBucket.low,
                    )
                ]
        return []

    def _income_amounts(
        self, inp: ExtractionInput, anchors: tuple[str, ...]
    ) -> list[ExtractedField]:
        fields: list[ExtractedField] = []
        seen: set[float] = set()
        for value, src in _anchored_kv(inp.text, inp.lines, anchors, inp.lang):
            amt_match = _AMOUNT_RE.search(value)
            if amt_match is None:
                continue
            amount = _clean_amount(amt_match.group(1))
            if amount is None or amount in seen:
                continue
            seen.add(amount)
            fields.append(
                _make_field(
                    attribute_key="income.amount",
                    value_num=amount,
                    raw_ocr=src.text if src is not None else value,
                    confidence=0.6,
                    sensitivity=SensitivityBucket.medium,
                )
            )
        return fields

    def _mailing_address(self, inp: ExtractionInput) -> list[ExtractedField]:
        for value, src in _anchored_kv(inp.text, inp.lines, _ADDRESS_ANCHORS_EN, inp.lang):
            cleaned = value.strip()
            if cleaned and len(cleaned) > 3:
                return [
                    _make_field(
                        attribute_key="address.mailing",
                        value=cleaned,
                        raw_ocr=src.text if src is not None else cleaned,
                        confidence=0.4,
                        sensitivity=SensitivityBucket.medium,
                    )
                ]
        return []

    def _first_date(self, text: str) -> str | None:
        match = _DATE_HINT_RE.search(text)
        if match is None:
            return None
        raw = match.group(1)
        try:
            import dateparser
        except ImportError:
            return raw
        parsed = dateparser.parse(raw)
        if parsed is None:
            return raw
        return parsed.date().isoformat()


# Singleton instance + registry hook.
EXTRACTOR = CanadaExtractor()
register(EXTRACTOR)
