"""Gate pipeline orchestration — Stages -0..3 collapsed into one :class:`GateResult`.

This is the single in-memory entry point that turns OCR output into a routing decision. It wires
together the four gate sub-stages, each of which lives in its own sibling module:

* **Language** (:mod:`di.gate.language`)  — dominant language + bilingual spans.
* **Anchors / classifier** (:mod:`di.gate.anchors`, :mod:`di.gate.classifier`) — doc-type label
  (the classifier defers to the anchor sweep internally when no trained model is present).
* **PII / sensitivity** (:mod:`di.gate.pii`) — detected entities + the max sensitivity bucket.
* **Routing** (:mod:`di.gate.routing`) — the fail-safe egress decision.

The gate is **local-only**: every sub-stage uses local models / heuristics with no network and no
DB, so :func:`run_gate` is synchronous. Each sub-stage already degrades gracefully when its optional
ML dependency is missing (lingua / scikit-learn / Presidio); this module additionally guards the
classifier and PII calls so an unexpected failure inside an optional path still yields a usable,
fail-safe :class:`GateResult` rather than raising out of the gate.
"""
from __future__ import annotations

import logging

from di.config import Settings, get_settings
from di.gate import anchors as anchors_mod
from di.gate import language as language_mod
from di.gate import pii as pii_mod
from di.gate import routing as routing_mod
from di.gate.classifier import UNKNOWN_DOC_TYPE, DocTypeClassifier
from di.models import (
    Classification,
    GateDecision,
    GateResult,
    LangProfile,
    OcrResult,
    PiiEntity,
    SensitivityBucket,
)

__all__ = ["run_gate"]

logger = logging.getLogger(__name__)


def _classify(text: str, lang: str) -> Classification:
    """Doc-type classification, guarded so an optional-dep failure degrades to UNKNOWN.

    The trained-model path and the anchor fallback are both internally guarded by
    :class:`DocTypeClassifier`; this extra guard catches anything unexpected (e.g. the
    concurrently-authored anchor module being absent) so the gate never raises here.
    """
    try:
        return DocTypeClassifier().predict(text, lang)
    except Exception as e:  # noqa: BLE001 - never let classification break the gate
        logger.warning("classifier failed; using UNKNOWN classification: %s", e)
        return Classification(doc_type=UNKNOWN_DOC_TYPE, confidence=0.0)


def _scan_pii(
    text: str, lang_profile: LangProfile
) -> tuple[list[PiiEntity], SensitivityBucket]:
    """PII + sensitivity scan, guarded so an optional-dep failure fails safe to CRITICAL.

    ``scan_pii`` already falls back to a deterministic regex sweep when Presidio/spaCy are absent;
    if even that raises unexpectedly we return no entities but the most conservative bucket so the
    routing gate keeps such a document on the deterministic-only path.
    """
    try:
        return pii_mod.scan_pii(text, lang_profile)
    except Exception as e:  # noqa: BLE001 - fail safe: treat as maximally sensitive
        logger.warning("PII scan failed; failing safe to CRITICAL sensitivity: %s", e)
        return [], SensitivityBucket.critical


def run_gate(ocr: OcrResult, *, settings: Settings | None = None) -> GateResult:
    """Run the full gate over an :class:`OcrResult` and return a single :class:`GateResult`.

    Steps (all local, no network/DB):

    1. Detect the dominant language and bilingual spans.
    2. Sweep anchors (kept for signal/audit) and classify the doc-type (classifier falls back to
       anchors internally).
    3. Scan for PII and resolve the sensitivity bucket.
    4. Route: decide egress using the settings' gate switch and confidence floor.

    Args:
        ocr: OCR output whose ``text`` (and, transitively, ``lines``) feeds every sub-stage.
        settings: Optional pre-resolved :class:`~di.config.Settings`; defaults to ``get_settings()``.

    Returns:
        A populated :class:`~di.models.GateResult`. The decision is fail-safe: anything not
        confidently classified and plainly LOW sensitivity stays ``DETERMINISTIC_ONLY``.
    """
    settings = settings if settings is not None else get_settings()
    text = ocr.text or ""

    lang_profile = language_mod.detect_language(text)

    # Anchor sweep is informational here (the classifier consults anchors itself), but running it
    # keeps the signal available for audit/debugging without affecting the decision.
    anchors_mod.classify_by_anchors(text, lang_profile.dominant_lang)

    classification = _classify(text, lang_profile.dominant_lang)
    pii_entities, sensitivity = _scan_pii(text, lang_profile)

    decision, rationale = routing_mod.route(
        classification,
        sensitivity,
        gate_open=settings.gate_default_open,
        conf_floor=settings.classifier_confidence_floor,
    )

    return GateResult(
        classification=classification,
        lang_profile=lang_profile,
        pii_entities=pii_entities,
        sensitivity=sensitivity,
        decision=decision,
        rationale=rationale,
    )


# Re-exported for callers/tests that want to assert the fail-safe default explicitly.
_FAIL_SAFE_DECISION = GateDecision.deterministic_only
