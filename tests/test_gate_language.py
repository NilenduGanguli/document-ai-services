"""Unit tests for di.gate.language.

The lingua-backed path is exercised only when ``lingua`` is installed (``importorskip``); the
deterministic fallback path is always tested directly so the suite is meaningful offline.
"""
from __future__ import annotations

import pytest

from di.gate import language as lang_mod
from di.models import LangProfile


# ---------------------------------------------------------------------------
# Public entry point — always available (uses fallback when lingua is absent).
# ---------------------------------------------------------------------------
def test_detect_language_returns_langprofile() -> None:
    profile = lang_mod.detect_language("The applicant submitted a passport for verification.")
    assert isinstance(profile, LangProfile)
    assert profile.dominant_lang in ("en", "es")
    assert profile.spans
    # Spans must carry valid offsets within the text.
    for span in profile.spans:
        assert 0 <= span.start <= span.end


def test_detect_language_empty_and_whitespace() -> None:
    for text in ("", "   \n\t  "):
        profile = lang_mod.detect_language(text)
        assert profile.dominant_lang == "en"
        assert profile.is_bilingual is False
        assert profile.spans == []


# ---------------------------------------------------------------------------
# Deterministic fallback path (private), tested directly so it runs without lingua.
# ---------------------------------------------------------------------------
def test_fallback_detects_english() -> None:
    candidates = lang_mod._detect_with_heuristic(
        "The applicant provided proof of address and a bank statement for the account."
    )
    profile = lang_mod._profile_from_candidates(
        "The applicant provided proof of address and a bank statement for the account.",
        candidates,
    )
    assert profile.dominant_lang == "en"
    assert profile.is_bilingual is False
    assert len(profile.spans) == 1


def test_fallback_detects_spanish_via_stopwords_and_diacritics() -> None:
    text = (
        "La credencial para votar fue emitida por el Instituto Nacional Electoral con "
        "fecha de nacimiento y domicilio del solicitante."
    )
    candidates = lang_mod._detect_with_heuristic(text)
    profile = lang_mod._profile_from_candidates(text, candidates)
    assert profile.dominant_lang == "es"
    assert profile.spans[0].start == 0
    assert profile.spans[0].end == len(text)


def test_fallback_via_monkeypatched_missing_lingua(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the lingua import to fail and confirm detect_language still returns a profile."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "lingua" or name.startswith("lingua."):
            raise ImportError("simulated missing lingua")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # _detect_with_lingua must signal unavailability with None (not an empty list).
    assert lang_mod._detect_with_lingua("hello world") is None

    profile = lang_mod.detect_language("Hola, este es un documento en español. Number account.")
    assert isinstance(profile, LangProfile)
    assert profile.dominant_lang in ("en", "es")
    assert profile.spans


def test_profile_bilingual_flag_from_candidates() -> None:
    """A credible secondary-language span flips is_bilingual."""
    text = "x" * 200
    candidates = [
        lang_mod._SpanCandidate(start=0, end=100, lang="en", confidence=0.95),
        lang_mod._SpanCandidate(start=100, end=200, lang="es", confidence=0.90),
    ]
    profile = lang_mod._profile_from_candidates(text, candidates)
    assert profile.is_bilingual is True
    assert {s.lang for s in profile.spans} == {"en", "es"}


def test_profile_not_bilingual_when_secondary_span_too_short() -> None:
    text = "y" * 120
    candidates = [
        lang_mod._SpanCandidate(start=0, end=110, lang="en", confidence=0.95),
        lang_mod._SpanCandidate(start=110, end=120, lang="es", confidence=0.95),  # 10 chars < 25
    ]
    profile = lang_mod._profile_from_candidates(text, candidates)
    assert profile.is_bilingual is False
    assert profile.dominant_lang == "en"


def test_profile_dominant_by_coverage() -> None:
    text = "z" * 300
    candidates = [
        lang_mod._SpanCandidate(start=0, end=100, lang="en", confidence=0.9),
        lang_mod._SpanCandidate(start=100, end=300, lang="es", confidence=0.9),
    ]
    profile = lang_mod._profile_from_candidates(text, candidates)
    assert profile.dominant_lang == "es"  # 200 chars vs 100


# ---------------------------------------------------------------------------
# Real lingua path — only when the optional dependency is installed.
# ---------------------------------------------------------------------------
def test_lingua_path_bilingual_en_es() -> None:
    pytest.importorskip("lingua")
    text = (
        "This is the English portion of the document describing the applicant. "
        "Esta es la parte en español que describe al solicitante y su domicilio fiscal."
    )
    profile = lang_mod.detect_language(text)
    assert isinstance(profile, LangProfile)
    assert profile.dominant_lang in ("en", "es")
    assert profile.spans
    detected = {s.lang for s in profile.spans}
    # Both languages should surface in the spans for a clearly bilingual document.
    assert "en" in detected and "es" in detected
    assert profile.is_bilingual is True


def test_lingua_path_monolingual_english() -> None:
    pytest.importorskip("lingua")
    profile = lang_mod.detect_language(
        "The applicant submitted a valid passport and proof of residential address."
    )
    assert profile.dominant_lang == "en"
    assert profile.spans
