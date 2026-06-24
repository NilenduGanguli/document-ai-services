"""Stage-0 cheap gate: anchor-keyword classification + checksummed ID regex sweep.

Pure and dependency-light. Uses ``python-stdnum`` (always installed) for ID checksum/structure
validation, importing the individual country submodules lazily inside :func:`detect_ids` so this
module imports cleanly even on a stripped-down install. No heavy ML deps, no network, no DB.

Two public functions:

* :func:`classify_by_anchors` - score each ontology doc-type by counting high-specificity anchor
  hits (EN + ES, case-insensitive) and return ranked :class:`~di.models.Classification` objects.
* :func:`detect_ids` - regex-sweep the text for SSN / SIN / CURP / RFC / EIN and the passport MRZ
  start line, returning only the matches that pass their checksum / structural validation.
"""
from __future__ import annotations

import re

from di.models import Classification
from di.ontology import DOC_TYPE_BY_CODE, anchors_for

# ---------------------------------------------------------------------------
# Anchor classification
# ---------------------------------------------------------------------------
# Languages whose anchor sets we sweep. ``anchors_for`` returns EN anchors for anything that is not
# "es", so we explicitly cover both registered languages.
_ANCHOR_LANGS: tuple[str, ...] = ("en", "es")

# A confidence ceiling so a flood of generic single-word anchors never pins us at a hard 1.0.
_CONFIDENCE_CEILING: float = 0.97


def _normalize(text: str) -> str:
    """Lower-case the haystack for case-insensitive substring matching."""
    return text.lower()


def _anchor_specificity(anchor: str) -> float:
    """Weight an anchor by how discriminating it is.

    Longer, multi-word header strings (e.g. "INSTITUTO NACIONAL ELECTORAL") are far more specific
    than short tokens (e.g. "DL", "SIN", "RFC"), so they contribute more confidence per hit.
    """
    a = anchor.strip()
    n_words = len(a.split())
    length = len(a)
    if n_words >= 3 or length >= 18:
        return 1.0
    if n_words == 2 or length >= 10:
        return 0.6
    if length >= 5:
        return 0.35
    # Very short tokens ("DL", "SIN", "EIN", "RFC", "P<", "CRA") are weak on their own.
    return 0.15


def _jurisdiction_guess(code: str, matched_langs: set[str]) -> str | None:
    """Best-guess jurisdiction for a matched doc-type.

    Single-jurisdiction specs resolve to that jurisdiction. Multi-jurisdiction specs use the
    matched anchor language as a tie-breaker (Spanish hit -> MX) and otherwise fall back to the
    first declared jurisdiction.
    """
    spec = DOC_TYPE_BY_CODE.get(code)
    if spec is None or not spec.jurisdictions:
        return None
    if len(spec.jurisdictions) == 1:
        return spec.jurisdictions[0]
    if "es" in matched_langs and "MX" in spec.jurisdictions:
        return "MX"
    return spec.jurisdictions[0]


def classify_by_anchors(text: str, lang: str = "en") -> list[Classification]:
    """Classify ``text`` by counting high-specificity anchor hits per ontology doc-type.

    Both English and Spanish anchor sets are always swept (case-insensitively) regardless of the
    ``lang`` hint, since KYC documents are frequently bilingual; ``lang`` only nudges the
    jurisdiction guess for multi-jurisdiction doc-types when Spanish anchors fire.

    Args:
        text: The OCR / extracted document text to classify.
        lang: Caller's language hint ("en" or "es"); used only as a jurisdiction tie-breaker.

    Returns:
        Ranked (highest confidence first) :class:`~di.models.Classification` objects, one per
        doc-type that had at least one anchor hit. Empty list if nothing matched.
    """
    if not text:
        return []

    haystack = _normalize(text)

    # Accumulate per-doc-type: matched anchor strings, summed specificity, languages that hit.
    scores: dict[str, float] = {}
    signals: dict[str, list[str]] = {}
    langs_hit: dict[str, set[str]] = {}

    langs = (lang,) if lang in ("en", "es") else ()
    sweep_langs = tuple(dict.fromkeys((*langs, *_ANCHOR_LANGS)))  # de-dup, preserve order

    for sweep_lang in sweep_langs:
        for code, anchors in anchors_for(sweep_lang).items():
            for anchor in anchors:
                needle = anchor.lower()
                if not needle:
                    continue
                if needle in haystack:
                    scores[code] = scores.get(code, 0.0) + _anchor_specificity(anchor)
                    bucket = signals.setdefault(code, [])
                    if anchor not in bucket:
                        bucket.append(anchor)
                    langs_hit.setdefault(code, set()).add(sweep_lang)

    if not scores:
        return []

    results: list[Classification] = []
    for code, raw_score in scores.items():
        # Squash the unbounded specificity sum into a 0..1 confidence. A single very-specific
        # anchor (weight 1.0) already lands at ~0.5; two strong hits push well past the floor.
        confidence = min(_CONFIDENCE_CEILING, 1.0 - 0.5 ** raw_score)
        spec = DOC_TYPE_BY_CODE.get(code)
        results.append(
            Classification(
                doc_type=code,
                doc_category=spec.category if spec is not None else None,
                jurisdiction=_jurisdiction_guess(code, langs_hit.get(code, set())),
                confidence=round(confidence, 4),
                signals=signals.get(code, []),
            )
        )

    results.sort(key=lambda c: (c.confidence, len(c.signals)), reverse=True)
    return results


# ---------------------------------------------------------------------------
# ID regex sweep + checksum validation
# ---------------------------------------------------------------------------
# Candidate patterns. We deliberately cast a slightly wide net here and rely on the stdnum
# validators below to discard structurally-invalid hits. Keys mirror di.ontology attribute keys
# where one exists (id.ssn, id.sin, id.curp, id.rfc, id.ein) plus "passport_mrz" for the MRZ line.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SIN_RE = re.compile(r"\b\d{3}-\d{3}-\d{3}\b|\b\d{9}\b")
_CURP_RE = re.compile(r"\b[A-Z]{4}\d{6}[HM][A-Z]{5}[0-9A-Z]\d\b")
_RFC_RE = re.compile(r"\b[A-ZÑ&]{3,4}\d{6}[0-9A-Z]{2}[0-9A]\b")
_EIN_RE = re.compile(r"\b\d{2}-\d{7}\b")
# ICAO 9303 passport MRZ first line: "P<" + 3-letter issuing-country code. No trailing word
# boundary: on a real MRZ the country code runs straight into the surname field (e.g. "P<USAPEREZ").
_MRZ_RE = re.compile(r"\bP[<K]([A-Z]{3})")

# Common ISO 3166-1 alpha-3 codes that legitimately appear after "P<" on KYC passports.
_MRZ_ALLOWED_NATIONS = {"USA", "CAN", "MEX"}


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving de-duplication."""
    return list(dict.fromkeys(values))


def detect_ids(text: str) -> dict[str, list[str]]:
    """Sweep ``text`` for government IDs and return only checksum/structure-valid matches.

    Each candidate found by a regex is validated with ``python-stdnum`` (lazily imported). Only the
    matches that pass validation are returned, keyed by ID type. Values are normalized to their
    ``stdnum`` compact form (digits/letters, dashes stripped) so downstream comparison is stable.

    Returns:
        Mapping of ID type -> list of validated, de-duplicated ID strings. ID types with no valid
        match are omitted entirely (no empty lists).
    """
    if not text:
        return {}

    # Lazy import: keep module import cheap and tolerant of a stripped stdnum install.
    try:
        from stdnum.ca import sin as ca_sin
        from stdnum.mx import curp as mx_curp
        from stdnum.mx import rfc as mx_rfc
        from stdnum.us import ein as us_ein
        from stdnum.us import ssn as us_ssn
    except ImportError:
        # Fallback: no stdnum -> we cannot checksum-validate, so we emit nothing rather than
        # returning unverified IDs from the sweep.
        return {}

    upper = text.upper()
    out: dict[str, list[str]] = {}

    def _collect(key: str, candidates: list[str], validate) -> None:
        valid: list[str] = []
        for cand in candidates:
            try:
                valid.append(validate(cand))
            except Exception:
                # stdnum raises ValidationError subclasses (InvalidComponent, InvalidChecksum,
                # InvalidFormat, InvalidLength). Anything that doesn't validate is dropped.
                continue
        if valid:
            out[key] = _dedupe(valid)

    # SSN (US) - 9 digits, dashed form only to avoid swallowing every 9-digit run.
    _collect("ssn", _SSN_RE.findall(text), us_ssn.validate)

    # SIN (CA) - Luhn checksum over 9 digits (dashed or plain).
    _collect("sin", _SIN_RE.findall(text), ca_sin.validate)

    # CURP (MX) - 18 chars with a trailing check digit (validate_check_digits defaults True).
    _collect("curp", _CURP_RE.findall(upper), mx_curp.validate)

    # RFC (MX) - structure check only; the homoclave check digit is unreliable in OCR, so we
    # follow the ontology hint and skip check-digit validation.
    _collect(
        "rfc",
        _RFC_RE.findall(upper),
        lambda c: mx_rfc.validate(c, validate_check_digits=False),
    )

    # EIN (US) - 2-digit IRS campus prefix + 7 digits; stdnum validates the prefix range.
    _collect("ein", _EIN_RE.findall(text), us_ein.validate)

    # Passport MRZ start - structural only (no stdnum); require a recognised issuing nation.
    # ``findall`` yields the captured 3-letter nation code; normalize to the canonical "P<XXX".
    mrz_hits = [f"P<{nation}" for nation in _MRZ_RE.findall(upper) if nation in _MRZ_ALLOWED_NATIONS]
    if mrz_hits:
        out["passport_mrz"] = _dedupe(mrz_hits)

    return out
