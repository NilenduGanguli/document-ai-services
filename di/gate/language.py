"""Language detection for the gate (English vs Spanish, with bilingual span support).

The pipeline is scoped to US/CA/MX KYC documents, which are overwhelmingly English and
Spanish (often bilingual on a single page — e.g. a US W-2 or a Canadian T4 with French is
out of scope here; we treat anything non-Spanish as English for routing purposes).

Public entry point: :func:`detect_language`, returning a :class:`~di.models.LangProfile`.

The high-accuracy path uses ``lingua`` (optional ``[ml]`` dependency). When ``lingua`` is not
installed the module still imports cleanly and falls back to a light stopword heuristic so the
gate keeps working offline. ``lingua`` is imported lazily *inside* the detector function — never
at module top — so importing this module never requires the heavy dependency.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from di.models import LangProfile, LangSpan

__all__ = ["detect_language"]

# Languages this gate distinguishes. Everything else collapses to the dominant of these two.
_SUPPORTED = ("en", "es")
_DEFAULT_LANG = "en"

# A bilingual document needs a *secondary* language span that is both long enough and confident
# enough to be real (guards against a stray loan-word flipping the flag).
_BILINGUAL_MIN_SPAN_CHARS = 25
_BILINGUAL_MIN_CONFIDENCE = 0.50

# ---------------------------------------------------------------------------
# Fallback heuristic data — small, high-signal stopword sets. Intentionally tiny: this only has
# to break the EN/ES tie well enough for routing; lingua does the real work when present.
# ---------------------------------------------------------------------------
_EN_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "of", "to", "in", "is", "was", "for", "with", "this", "that", "are",
        "be", "by", "on", "as", "at", "from", "or", "an", "name", "date", "birth", "address",
        "number", "issued", "expiry", "expires", "statement", "account", "passport", "license",
        "social", "security", "driver", "income", "tax",
    }
)
_ES_STOPWORDS: frozenset[str] = frozenset(
    {
        "el", "la", "los", "las", "de", "del", "y", "en", "es", "un", "una", "con", "por",
        "para", "que", "se", "su", "como", "fecha", "nacimiento", "nombre", "domicilio",
        "numero", "número", "credencial", "registro", "federal", "contribuyentes", "estado",
        "cuenta", "constancia", "situación", "situacion", "fiscal", "clave", "población",
        "poblacion", "nacional", "electoral",
    }
)
# Characters that are strong Spanish signals on their own.
_ES_CHARS = re.compile(r"[ñáéíóúü¿¡]", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-zA-ZñáéíóúüÑÁÉÍÓÚÜ]+", re.UNICODE)


@dataclass(frozen=True)
class _SpanCandidate:
    """A contiguous run of text attributed to one language, with a confidence in [0, 1]."""

    start: int
    end: int
    lang: str
    confidence: float


def detect_language(text: str) -> LangProfile:
    """Detect the dominant language and any bilingual spans for ``text``.

    Returns a :class:`~di.models.LangProfile` whose ``dominant_lang`` is one of ``"en"``/``"es"``,
    ``spans`` covers the input (character offsets), and ``is_bilingual`` is set when a credible
    secondary-language span is present.

    Uses ``lingua`` when installed; otherwise a deterministic stopword heuristic.
    """
    if not text or not text.strip():
        return LangProfile(dominant_lang=_DEFAULT_LANG, is_bilingual=False, spans=[])

    candidates = _detect_with_lingua(text)
    if candidates is None:
        candidates = _detect_with_heuristic(text)
    return _profile_from_candidates(text, candidates)


def _detect_with_lingua(text: str) -> list[_SpanCandidate] | None:
    """High-accuracy path. Returns ``None`` (not an empty list) when ``lingua`` is unavailable.

    ``lingua`` is imported here, lazily, so the module imports without the optional dependency.
    """
    try:
        from lingua import Language, LanguageDetectorBuilder
    except ImportError:
        return None

    detector = LanguageDetectorBuilder.from_languages(
        Language.ENGLISH, Language.SPANISH
    ).build()

    candidates: list[_SpanCandidate] = []
    try:
        results = detector.detect_multiple_languages_of(text)
    except Exception:
        # Defensive: never let a detector hiccup crash the gate — fall through to heuristic.
        return None

    for res in results:
        lang = _lingua_lang_to_iso(res.language)
        confidence = _confidence_for_segment(detector, text, res.start_index, res.end_index, lang)
        candidates.append(
            _SpanCandidate(
                start=res.start_index,
                end=res.end_index,
                lang=lang,
                confidence=confidence,
            )
        )

    if not candidates:
        # Single-language fallback when multi-language detection yields nothing.
        lang = _lingua_dominant(detector, text)
        candidates = [_SpanCandidate(start=0, end=len(text), lang=lang, confidence=1.0)]
    return candidates


def _lingua_lang_to_iso(language: object) -> str:
    """Map a lingua ``Language`` to our 'en'/'es' code, defaulting to English."""
    try:
        code = language.iso_code_639_1.name.lower()  # type: ignore[attr-defined]
    except AttributeError:
        code = _DEFAULT_LANG
    return code if code in _SUPPORTED else _DEFAULT_LANG


def _confidence_for_segment(
    detector: object, text: str, start: int, end: int, lang: str
) -> float:
    """Confidence that ``text[start:end]`` is ``lang`` per lingua's confidence values."""
    segment = text[start:end]
    if not segment.strip():
        return 0.0
    try:
        values = detector.compute_language_confidence_values(segment)  # type: ignore[attr-defined]
    except Exception:
        return 1.0
    for cv in values:
        if _lingua_lang_to_iso(cv.language) == lang:
            return float(cv.value)
    return 0.0


def _lingua_dominant(detector: object, text: str) -> str:
    """Dominant language over the whole text via confidence values; defaults to English."""
    try:
        values = detector.compute_language_confidence_values(text)  # type: ignore[attr-defined]
    except Exception:
        return _DEFAULT_LANG
    best_lang = _DEFAULT_LANG
    best_value = -1.0
    for cv in values:
        if cv.value > best_value:
            best_value = cv.value
            best_lang = _lingua_lang_to_iso(cv.language)
    return best_lang


def _detect_with_heuristic(text: str) -> list[_SpanCandidate]:
    """Deterministic stopword/character heuristic used when ``lingua`` is absent.

    Produces a single span covering the whole text. Spanish is chosen when its stopword/char
    score outweighs English; otherwise English (the routing default).
    """
    en_score, es_score = _score_es_vs_en(text)
    if es_score > en_score:
        lang = "es"
        total = en_score + es_score
        confidence = es_score / total if total else 0.5
    else:
        lang = "en"
        total = en_score + es_score
        confidence = en_score / total if total else 0.5
    return [_SpanCandidate(start=0, end=len(text), lang=lang, confidence=confidence)]


def _score_es_vs_en(text: str) -> tuple[float, float]:
    """Return ``(en_score, es_score)`` from stopword hits plus Spanish-character weighting."""
    words = [w.lower() for w in _WORD_RE.findall(text)]
    en_hits = sum(1 for w in words if w in _EN_STOPWORDS)
    es_hits = sum(1 for w in words if w in _ES_STOPWORDS)
    # Spanish diacritics / inverted punctuation are strong, EN-free signals.
    es_char_hits = len(_ES_CHARS.findall(text))
    return float(en_hits), float(es_hits) + 1.5 * float(es_char_hits)


def _profile_from_candidates(text: str, candidates: list[_SpanCandidate]) -> LangProfile:
    """Collapse span candidates into a :class:`LangProfile`.

    Dominant language is the one covering the most characters. ``is_bilingual`` is set when a
    credible secondary-language span (length and confidence above threshold) exists.
    """
    if not candidates:
        return LangProfile(
            dominant_lang=_DEFAULT_LANG,
            is_bilingual=False,
            spans=[LangSpan(start=0, end=len(text), lang=_DEFAULT_LANG)],
        )

    coverage: dict[str, int] = {}
    for c in candidates:
        coverage[c.lang] = coverage.get(c.lang, 0) + max(0, c.end - c.start)
    # Deterministic tie-break: most coverage, then English preferred, then alphabetical.
    dominant = max(
        coverage,
        key=lambda lang: (coverage[lang], lang == _DEFAULT_LANG, lang),
    )

    credible_langs = {
        c.lang
        for c in candidates
        if (c.end - c.start) >= _BILINGUAL_MIN_SPAN_CHARS
        and c.confidence >= _BILINGUAL_MIN_CONFIDENCE
    }
    is_bilingual = len({lang for lang in credible_langs if lang in _SUPPORTED} | {dominant}) > 1

    spans = [LangSpan(start=c.start, end=c.end, lang=c.lang) for c in candidates]
    return LangProfile(dominant_lang=dominant, is_bilingual=is_bilingual, spans=spans)
