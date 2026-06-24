"""Unit tests for the doc-type classifier (di.gate.classifier).

The anchor-fallback path is tested directly by monkeypatching ``di.gate.anchors.classify_by_anchors``
(the sibling module may be authored concurrently). The trained scikit-learn path is guarded by
``pytest.importorskip('sklearn')`` so it skips cleanly when the optional ML group is absent.
"""
from __future__ import annotations

import sys

import pytest

from di.gate.classifier import UNKNOWN_DOC_TYPE, DocTypeClassifier
from di.models import Classification


def _install_anchor_stub(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    """Patch the exact symbol the classifier calls: ``di.gate.anchors.classify_by_anchors``.

    setattr on the real (already-imported) module is robust to import order, unlike swapping
    ``sys.modules`` — ``from di.gate import anchors`` resolves the cached submodule attribute.
    """
    import di.gate.anchors as anchors_mod

    monkeypatch.setattr(anchors_mod, "classify_by_anchors", fn)


def test_fallback_returns_top_anchor_result(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = Classification(doc_type="MX_CURP", confidence=0.81, signals=["anchor:CURP"])
    calls: list[tuple[str, str]] = []

    def stub(text: str, lang: str = "en"):
        calls.append((text, lang))
        return [expected, Classification(doc_type="MX_INE", confidence=0.4)]

    _install_anchor_stub(monkeypatch, stub)

    clf = DocTypeClassifier()  # no model path -> fallback
    out = clf.predict("CLAVE UNICA DE REGISTRO DE POBLACION", lang="es")

    assert out is expected
    assert out.doc_type == "MX_CURP"
    assert out.confidence == pytest.approx(0.81)
    assert calls == [("CLAVE UNICA DE REGISTRO DE POBLACION", "es")]


def test_fallback_unknown_when_no_anchor_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_anchor_stub(monkeypatch, lambda text, lang="en": [])

    clf = DocTypeClassifier()
    out = clf.predict("totally unrecognisable content")

    assert out.doc_type == UNKNOWN_DOC_TYPE
    assert out.confidence == 0.0


def test_fallback_coerces_tuple_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anchor module returning (doc_type, confidence) tuples is coerced robustly."""
    _install_anchor_stub(monkeypatch, lambda text, lang="en": [("US_W2", 0.66), ("US_1099", 0.2)])

    clf = DocTypeClassifier()
    out = clf.predict("WAGE AND TAX STATEMENT")

    assert isinstance(out, Classification)
    assert out.doc_type == "US_W2"
    assert out.confidence == pytest.approx(0.66)


def test_fallback_coerces_single_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single Classification (not wrapped in a list) is accepted."""
    single = Classification(doc_type="PASSPORT", confidence=0.95)
    _install_anchor_stub(monkeypatch, lambda text, lang="en": single)

    clf = DocTypeClassifier()
    out = clf.predict("P<USA")

    assert out.doc_type == "PASSPORT"
    assert out.confidence == pytest.approx(0.95)


def test_missing_anchors_module_returns_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """If di.gate.anchors cannot be imported, predict degrades to UNKNOWN, never raises."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, globals=None, locals=None, fromlist=(), level=0):
        if name == "di.gate.anchors" or (name == "di.gate" and "anchors" in (fromlist or ())):
            raise ImportError("anchors not authored yet")
        return real_import(name, globals, locals, fromlist, level)

    # Ensure no cached stub exists.
    monkeypatch.delitem(sys.modules, "di.gate.anchors", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    clf = DocTypeClassifier()
    out = clf.predict("anything")

    assert out.doc_type == UNKNOWN_DOC_TYPE
    assert out.confidence == 0.0


def test_nonexistent_model_path_uses_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A configured-but-missing model file silently falls back to anchors."""
    expected = Classification(doc_type="CA_T4", confidence=0.5)
    _install_anchor_stub(monkeypatch, lambda text, lang="en": [expected])

    clf = DocTypeClassifier(model_path=tmp_path / "does_not_exist.joblib")
    out = clf.predict("STATEMENT OF REMUNERATION")

    assert out.doc_type == "CA_T4"


def test_train_and_predict_roundtrip(tmp_path) -> None:
    """Trained TF-IDF + calibrated LinearSVC path. Skips without scikit-learn/joblib."""
    pytest.importorskip("sklearn")
    pytest.importorskip("joblib")

    # CalibratedClassifierCV defaults to cv=5 -> need >=5 samples per class.
    passport_texts = [
        "PASSPORT United States of America P<USA",
        "PASSPORT type P code USA passport number",
        "PASSPORT republic passport machine readable zone P<",
        "PASSPORT date of expiry nationality given names",
        "PASSPORT surname given names passport number P<USA",
        "PASSPORT travel document P< nationality USA",
    ]
    w2_texts = [
        "Wage and Tax Statement W-2 OMB No. 1545-0008",
        "W-2 employer wages tips social security wages",
        "Wage and Tax Statement federal income tax withheld",
        "W-2 box 1 wages box 2 federal income tax",
        "Wage and Tax Statement employer identification number",
        "W-2 OMB 1545-0008 medicare wages and tips",
    ]
    texts = passport_texts + w2_texts
    labels = ["PASSPORT"] * len(passport_texts) + ["US_W2"] * len(w2_texts)

    out_path = tmp_path / "model.joblib"
    clf = DocTypeClassifier()
    clf.train(texts, labels, out_path)
    assert out_path.exists()

    res = clf.predict("PASSPORT P<USA nationality given names")
    assert res.doc_type == "PASSPORT"
    assert 0.0 <= res.confidence <= 1.0
    assert res.signals == ["model:tfidf+linsvc_calibrated"]

    # A freshly-constructed classifier loading the same file should agree.
    reloaded = DocTypeClassifier(model_path=out_path)
    res2 = reloaded.predict("Wage and Tax Statement W-2 OMB 1545-0008")
    assert res2.doc_type == "US_W2"
