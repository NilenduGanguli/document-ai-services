"""US deterministic extractor — SSN / EIN / ITIN checksums + anchored KV for forms.

Handles the US fixed-format / form document types:

* ``US_SSN_CARD``       — Social Security card (SSN, name)
* ``US_EIN_LETTER``     — IRS CP-575 EIN assignment letter (EIN, entity legal name)
* ``US_W2``             — Wage & Tax Statement (employer, wages, SSN, EIN)
* ``US_1099``           — 1099 income (amount, payer EIN, recipient SSN)
* ``US_DRIVER_LICENSE`` — Driver license (name, DOB, address, expiry)

Identifier extraction is a global regex sweep over the OCR text validated with
``python-stdnum`` (``stdnum.us.{ssn,ein,itin}``) so structurally-invalid or checksum-failing
numbers never surface. Names / addresses / amounts come from the sibling
``anchored_kv`` extractor when it is available; if that module is absent the extractor
degrades gracefully and still returns the validated identifiers.

Pure / offline: no DB, no network. ``python-stdnum`` is a light, always-installed dependency.
"""
from __future__ import annotations

import re

from di.extract.base import ExtractionInput, register
from di.models import (
    ExtractedField,
    ExtractionSource,
    SensitivityBucket,
    VerificationStatus,
)

# ---------------------------------------------------------------------------
# Regex sweeps. We deliberately over-capture by *shape*, then let stdnum decide
# validity. SSN and ITIN share the NNN-NN-NNNN shape; EIN is the distinct
# NN-NNNNNNN shape. \b boundaries keep us off longer digit runs.
# ---------------------------------------------------------------------------
_SSN_LIKE_RE = re.compile(r"(?<!\d)(\d{3}-\d{2}-\d{4})(?!\d)")
_EIN_LIKE_RE = re.compile(r"(?<!\d)(\d{2}-\d{7})(?!\d)")

#: doc_type codes whose primary subject identifier is the EIN (payer / entity side).
_EIN_PRIMARY = frozenset({"US_EIN_LETTER", "US_W2", "US_1099"})


class UsDeterministicExtractor:
    """Offline extractor for US identity / tax / income document types."""

    handles: frozenset[str] = frozenset(
        {"US_SSN_CARD", "US_EIN_LETTER", "US_W2", "US_1099", "US_DRIVER_LICENSE"}
    )

    def extract(self, inp: ExtractionInput) -> list[ExtractedField]:
        fields: list[ExtractedField] = []
        fields.extend(self._sweep_identifiers(inp.text))
        fields.extend(self._anchored_fields(inp))
        return fields

    # -- identifier sweep ---------------------------------------------------
    def _sweep_identifiers(self, text: str) -> list[ExtractedField]:
        from stdnum.us import ein as us_ein
        from stdnum.us import itin as us_itin
        from stdnum.us import ssn as us_ssn

        out: list[ExtractedField] = []
        seen: set[str] = set()

        # NN-NNNNNNN shape -> EIN
        for match in _EIN_LIKE_RE.finditer(text):
            raw = match.group(1)
            if not us_ein.is_valid(raw):
                continue
            compact = us_ein.compact(raw)
            key = f"id.ein:{compact}"
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ExtractedField(
                    attribute_key="id.ein",
                    value=us_ein.format(compact),
                    raw_ocr=raw,
                    source=ExtractionSource.regex_sweep,
                    checksum_ok=True,
                    verification_status=VerificationStatus.checksum_verified,
                    confidence=0.95,
                    sensitivity=SensitivityBucket.high,
                )
            )

        # NNN-NN-NNNN shape -> SSN or ITIN (mutually exclusive in stdnum)
        for match in _SSN_LIKE_RE.finditer(text):
            raw = match.group(1)
            if us_ssn.is_valid(raw):
                compact = us_ssn.compact(raw)
                attribute_key = "id.ssn"
                formatted = us_ssn.format(compact)
            elif us_itin.is_valid(raw):
                compact = us_itin.compact(raw)
                attribute_key = "id.itin"
                formatted = us_itin.format(compact)
            else:
                continue
            key = f"{attribute_key}:{compact}"
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ExtractedField(
                    attribute_key=attribute_key,
                    value=formatted,
                    raw_ocr=raw,
                    source=ExtractionSource.regex_sweep,
                    checksum_ok=True,
                    verification_status=VerificationStatus.checksum_verified,
                    confidence=0.95,
                    sensitivity=SensitivityBucket.critical,
                )
            )
        return out

    # -- anchored KV (names / addresses / amounts) --------------------------
    #: attribute_key -> candidate label strings (EN + ES) the anchored-KV helper fuzzy-matches.
    _SOFT_LABELS: dict[str, dict[str, tuple[str, ...]]] = {
        "US_DRIVER_LICENSE": {
            "identity.full_name": ("Name", "LN", "FN"),
            "identity.date_of_birth": ("DOB", "Date of Birth"),
            "address.residential": ("Address", "ADDR"),
            "doc.expiry_date": ("EXP", "Expires", "Expiration"),
            "id.driver_license": ("DL", "License", "License No", "DL No"),
        },
        "US_EIN_LETTER": {"entity.legal_name": ("Legal name", "Name", "Business name")},
        "US_W2": {
            "income.employer": ("Employer", "Employer's name", "Employer name"),
            "income.amount": ("Wages", "Wages, tips", "Box 1"),
        },
        "US_1099": {"income.amount": ("Nonemployee compensation", "Box 1", "Amount")},
        "US_SSN_CARD": {"identity.full_name": ("Name",)},
    }

    def _anchored_fields(self, inp: ExtractionInput) -> list[ExtractedField]:
        """Map soft (non-checksummed) fields via the shared anchored-KV helper.

        Calls ``di.extract.deterministic.anchored_kv.anchor_extract(lines, labels)`` which returns
        ``(matched_label, value_line)`` pairs, then maps each matched label back to its attribute
        key. Fully guarded: any failure yields nothing rather than breaking the identifier sweep.
        """
        label_map = self._SOFT_LABELS.get(inp.doc_type)
        if not label_map or not inp.lines:
            return []
        label_to_key: dict[str, str] = {
            label: key for key, labels in label_map.items() for label in labels
        }
        try:
            from di.extract.deterministic.anchored_kv import anchor_extract
        except ImportError:
            return []
        try:
            pairs = anchor_extract(inp.lines, list(label_to_key))
        except (TypeError, ValueError):
            return []
        out: list[ExtractedField] = []
        for matched_label, value_line in pairs:
            key = label_to_key.get(matched_label)
            value = (getattr(value_line, "text", "") or "").strip()
            if not key or not value:
                continue
            out.append(
                ExtractedField(
                    attribute_key=key,
                    value=value,
                    raw_ocr=value,
                    source=ExtractionSource.anchor,
                    verification_status=VerificationStatus.unverified,
                    confidence=0.6,
                    sensitivity=SensitivityBucket.medium,
                    bbox=getattr(value_line, "bbox", None),
                )
            )
        return out


#: Registered singleton — dispatch by doc_type happens through ``di.extract.base.get_extractor``.
US_EXTRACTOR = register(UsDeterministicExtractor())
