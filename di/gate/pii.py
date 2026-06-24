"""PII detection + sensitivity scoring for the gate.

Two paths, identical public interface:

* **Preferred** — lazy-import Presidio (``presidio_analyzer``) and build a multilingual
  ``AnalyzerEngine`` over ``['en', 'es']`` (spaCy ``en_core_web_lg`` + ``es_core_news_lg``),
  augmented with custom ``PatternRecognizer`` rules for Mexican CURP / RFC / INE Clave de Elector.
  Each ``LangSpan`` is analysed in its own language and the offsets are unioned back to absolute
  positions in ``text``.
* **Fallback** — Presidio / spaCy absent: a deterministic regex + ``python-stdnum`` sweep
  (SSN / SIN / CURP / RFC / EIN / email / phone). Validated national IDs win over weaker matches.

The module imports cleanly with neither Presidio nor spaCy installed. ``scan_pii`` always returns
``(list[PiiEntity], SensitivityBucket)`` — raw, in-memory, no DB, no network.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from di.models import LangProfile, LangSpan, PiiEntity, SensitivityBucket

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from presidio_analyzer import AnalyzerEngine

# ---------------------------------------------------------------------------
# Entity-type catalog & sensitivity policy
# ---------------------------------------------------------------------------
# National identifiers — any single one of these makes a document CRITICAL.
NATIONAL_ID_TYPES: frozenset[str] = frozenset(
    {
        "US_SSN",
        "SSN",
        "US_ITIN",
        "CA_SIN",
        "SIN",
        "MX_CURP",
        "CURP",
        "MX_RFC",
        "RFC",
        "MX_INE_CLAVE_ELECTOR",
        "INE_CLAVE_ELECTOR",
        "PASSPORT",
        "US_PASSPORT",
        "US_EIN",
        "EIN",
    }
)

# Identity / quasi-identifier types that, when they co-occur, raise the bucket.
PERSON_TYPES: frozenset[str] = frozenset({"PERSON", "PERSON_NAME"})
DOB_TYPES: frozenset[str] = frozenset({"DATE_OF_BIRTH", "DOB", "DATE_TIME"})
ADDRESS_TYPES: frozenset[str] = frozenset({"ADDRESS", "LOCATION", "US_ADDRESS"})

# Contact-only types — on their own these are LOW sensitivity.
CONTACT_TYPES: frozenset[str] = frozenset(
    {"EMAIL_ADDRESS", "EMAIL", "PHONE_NUMBER", "PHONE", "URL", "IP_ADDRESS"}
)


# ---------------------------------------------------------------------------
# Fallback regex catalog (Presidio / spaCy absent)
# ---------------------------------------------------------------------------
# Mexican identifiers are matched *before* the weaker patterns and validated via stdnum, so a
# valid CURP is never mis-tagged as an RFC (CURP's leading 13 chars resemble an RFC).
_RE_CURP = re.compile(r"\b([A-Z][AEIOUX][A-Z]{2}\d{6}[HM][A-Z]{5}[0-9A-Z]\d)\b")
_RE_RFC = re.compile(r"\b([A-ZÑ&]{3,4}\d{6}[0-9A-Z]{2}[0-9A])\b")
_RE_INE_CLAVE = re.compile(r"\b([A-Z]{6}\d{8}[HM]\d{3})\b")
_RE_SSN = re.compile(r"\b(\d{3}-\d{2}-\d{4})\b")
_RE_SIN = re.compile(r"\b(\d{3}-\d{3}-\d{3})\b")
_RE_EIN = re.compile(r"\b(\d{2}-\d{7})\b")
_RE_EMAIL = re.compile(r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
# Phone: NANP-ish / international, kept conservative to limit false positives.
_RE_PHONE = re.compile(
    r"(?<![\w-])(\+?\d{1,3}[\s.\-]?)?(\(?\d{3}\)?[\s.\-])\d{3}[\s.\-]\d{4}(?![\w-])"
)

# Spanish context tokens used to weight the (regex-only) INE recognizer.
_INE_CONTEXT_ES = ("clave de elector", "credencial para votar", "instituto nacional electoral")


def _stdnum_valid(module_path: str, value: str) -> bool:
    """Validate ``value`` with a ``python-stdnum`` submodule; absent module -> ``False``."""
    try:
        import importlib

        mod = importlib.import_module(module_path)
    except ImportError:  # pragma: no cover - stdnum is a hard dep, defensive only
        return False
    is_valid = getattr(mod, "is_valid", None)
    if is_valid is None:  # pragma: no cover - defensive
        return False
    try:
        return bool(is_valid(value))
    except Exception:  # noqa: BLE001 - any stdnum parse error means "not valid"
        return False


# ---------------------------------------------------------------------------
# Fallback path
# ---------------------------------------------------------------------------
def _scan_fallback(text: str, default_lang: str) -> list[PiiEntity]:
    """Deterministic regex + stdnum sweep. Validated national IDs claim their spans first."""
    entities: list[PiiEntity] = []
    claimed: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(start < c_end and end > c_start for c_start, c_end in claimed)

    def _add(entity_type: str, start: int, end: int, score: float, lang: str) -> None:
        if _overlaps(start, end):
            return
        claimed.append((start, end))
        entities.append(
            PiiEntity(entity_type=entity_type, start=start, end=end, score=score, lang=lang)
        )

    # 1) Checksummed national IDs (highest precedence; CURP before RFC).
    for m in _RE_CURP.finditer(text):
        if _stdnum_valid("stdnum.mx.curp", m.group(1)):
            _add("MX_CURP", m.start(1), m.end(1), 0.95, "es")
    for m in _RE_INE_CLAVE.finditer(text):
        ctx = text[max(0, m.start(1) - 60) : m.start(1)].lower()
        score = 0.85 if any(tok in ctx for tok in _INE_CONTEXT_ES) else 0.45
        _add("MX_INE_CLAVE_ELECTOR", m.start(1), m.end(1), score, "es")
    for m in _RE_RFC.finditer(text):
        if _stdnum_valid("stdnum.mx.rfc", m.group(1)):
            _add("MX_RFC", m.start(1), m.end(1), 0.9, "es")
    for m in _RE_SSN.finditer(text):
        score = 0.95 if _stdnum_valid("stdnum.us.ssn", m.group(1)) else 0.5
        _add("US_SSN", m.start(1), m.end(1), score, "en")
    for m in _RE_SIN.finditer(text):
        score = 0.9 if _stdnum_valid("stdnum.ca.sin", m.group(1)) else 0.45
        _add("CA_SIN", m.start(1), m.end(1), score, "en")
    for m in _RE_EIN.finditer(text):
        score = 0.85 if _stdnum_valid("stdnum.us.ein", m.group(1)) else 0.4
        _add("US_EIN", m.start(1), m.end(1), score, "en")

    # 2) Contact identifiers (LOW sensitivity).
    for m in _RE_EMAIL.finditer(text):
        _add("EMAIL_ADDRESS", m.start(1), m.end(1), 0.8, default_lang)
    for m in _RE_PHONE.finditer(text):
        _add("PHONE_NUMBER", m.start(), m.end(), 0.6, default_lang)

    entities.sort(key=lambda e: e.start)
    return entities


# ---------------------------------------------------------------------------
# Preferred path — Presidio (lazy, fully guarded)
# ---------------------------------------------------------------------------
def _build_analyzer() -> AnalyzerEngine | None:
    """Build a multilingual Presidio ``AnalyzerEngine`` (en + es) with custom MX recognizers.

    Returns ``None`` if Presidio / spaCy / the language models are unavailable, so callers fall
    back to the deterministic sweep. Everything here is import-guarded.
    """
    try:
        from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
        from presidio_analyzer.nlp_engine import NlpEngineProvider
    except ImportError:
        return None

    nlp_configuration = {
        "nlp_engine_name": "spacy",
        "models": [
            {"lang_code": "en", "model_name": "en_core_web_lg"},
            {"lang_code": "es", "model_name": "es_core_news_lg"},
        ],
    }
    try:
        provider = NlpEngineProvider(nlp_configuration=nlp_configuration)
        nlp_engine = provider.create_engine()
    except Exception:  # noqa: BLE001 - missing spaCy models / load failure -> fallback
        return None

    # Custom Mexican-ID recognizers. CURP / RFC carry strong regexes (stdnum-validated downstream
    # is not available in Presidio, so the pattern is the signal); INE leans on Spanish context
    # at a deliberately low base score to avoid false positives on generic alphanumerics.
    curp_recognizer = PatternRecognizer(
        supported_entity="MX_CURP",
        supported_language="es",
        patterns=[Pattern(name="curp", regex=_RE_CURP.pattern, score=0.85)],
        context=["curp", "registro de poblacion", "renapo"],
    )
    rfc_recognizer = PatternRecognizer(
        supported_entity="MX_RFC",
        supported_language="es",
        patterns=[Pattern(name="rfc", regex=_RE_RFC.pattern, score=0.8)],
        context=["rfc", "registro federal de contribuyentes", "sat"],
    )
    ine_recognizer = PatternRecognizer(
        supported_entity="MX_INE_CLAVE_ELECTOR",
        supported_language="es",
        patterns=[Pattern(name="ine_clave", regex=_RE_INE_CLAVE.pattern, score=0.35)],
        context=list(_INE_CONTEXT_ES),
    )

    try:
        engine = AnalyzerEngine(
            nlp_engine=nlp_engine, supported_languages=["en", "es"]
        )
        engine.registry.add_recognizer(curp_recognizer)
        engine.registry.add_recognizer(rfc_recognizer)
        engine.registry.add_recognizer(ine_recognizer)
    except Exception:  # noqa: BLE001 - engine assembly failure -> fallback
        return None
    return engine


def _scan_presidio(
    engine: AnalyzerEngine, text: str, spans: list[LangSpan], default_lang: str
) -> list[PiiEntity] | None:
    """Run Presidio per language span, union the offsets. ``None`` signals an internal failure."""
    if not spans:
        spans = [LangSpan(start=0, end=len(text), lang=default_lang)]

    seen: set[tuple[str, int, int]] = set()
    entities: list[PiiEntity] = []
    try:
        for span in spans:
            lang = span.lang if span.lang in ("en", "es") else default_lang
            results = engine.analyze(text=text, language=lang)
            for r in results:
                # Keep only hits that fall within this span's window.
                if r.start < span.start or r.end > span.end:
                    continue
                key = (r.entity_type, r.start, r.end)
                if key in seen:
                    continue
                seen.add(key)
                entities.append(
                    PiiEntity(
                        entity_type=r.entity_type,
                        start=r.start,
                        end=r.end,
                        score=float(r.score),
                        lang=lang,
                    )
                )
    except Exception:  # noqa: BLE001 - any analyzer error -> let caller fall back
        return None
    entities.sort(key=lambda e: e.start)
    return entities


# ---------------------------------------------------------------------------
# Sensitivity scoring
# ---------------------------------------------------------------------------
def score_sensitivity(entities: list[PiiEntity]) -> SensitivityBucket:
    """Map a set of PII entities to the max :class:`SensitivityBucket`.

    * any validated national ID (SSN/SIN/CURP/RFC/INE/passport/EIN) -> CRITICAL
    * PERSON + DOB + address co-occurrence -> HIGH; PERSON + (DOB or address) -> MEDIUM
    * lone email / phone -> LOW
    """
    if not entities:
        return SensitivityBucket.low

    types = {e.entity_type for e in entities}

    if types & NATIONAL_ID_TYPES:
        return SensitivityBucket.critical

    has_person = bool(types & PERSON_TYPES)
    has_dob = bool(types & DOB_TYPES)
    has_address = bool(types & ADDRESS_TYPES)

    if has_person and has_dob and has_address:
        return SensitivityBucket.high
    if has_person and (has_dob or has_address):
        return SensitivityBucket.medium
    if (has_dob and has_address) or has_address:
        # Quasi-identifiers without an explicit name still warrant a step up from LOW.
        return SensitivityBucket.medium

    return SensitivityBucket.low


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def scan_pii(
    text: str, lang_profile: LangProfile
) -> tuple[list[PiiEntity], SensitivityBucket]:
    """Detect PII in ``text`` and return ``(entities, max_sensitivity_bucket)``.

    Prefers a multilingual Presidio analyzer (per :class:`LangSpan`); transparently falls back to
    a deterministic regex + ``python-stdnum`` sweep when Presidio / spaCy (or its models) are
    unavailable. The result shape is identical on either path.
    """
    if not text:
        return [], SensitivityBucket.low

    default_lang = lang_profile.dominant_lang if lang_profile.dominant_lang else "en"

    entities: list[PiiEntity] | None = None
    engine = _build_analyzer()
    if engine is not None:
        entities = _scan_presidio(engine, text, lang_profile.spans, default_lang)

    if entities is None:
        entities = _scan_fallback(text, default_lang)

    return entities, score_sensitivity(entities)
