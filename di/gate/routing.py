"""Stage-3 routing-gate policy — decides whether a document may leave the deterministic path.

This is the single chokepoint that governs egress to the LLM gateway. It is intentionally
*pure* (no I/O, no DB, no network, no heavy deps) so it can be unit-tested exhaustively and
reasoned about as a fixed decision table.

The policy is **fail-safe**: when in doubt, keep the document on the deterministic-only path
(``DETERMINISTIC_ONLY``) rather than risk sending sensitive or mis-classified content to an
external model. The only paths that allow egress are:

* ``SEND_TO_LLM``      — low-sensitivity, confidently classified, and the gate is open.
* ``REDACT_THEN_SEND`` — sensitive but redaction is active, so PII is stripped before egress.

Precedence (highest wins):

1. Low-confidence / unknown classification on anything non-trivially sensitive -> deterministic.
2. CRITICAL or HIGH sensitivity -> deterministic, unless redaction is active (then redact).
3. MEDIUM sensitivity -> redact if active, else deterministic.
4. LOW sensitivity + confident + gate open -> send to the LLM.
5. Anything else / unexpected -> deterministic (fail safe).
"""
from __future__ import annotations

from di.models import Classification, GateDecision, SensitivityBucket

#: doc_type strings that signal an indeterminate classification (case-insensitive match).
_UNKNOWN_DOC_TYPES: frozenset[str] = frozenset({"", "unknown", "UNKNOWN", "unclassified"})


def _is_unknown_doc_type(doc_type: str | None) -> bool:
    """True when the classifier produced no usable doc_type label."""
    if doc_type is None:
        return True
    return doc_type.strip().casefold() in {t.casefold() for t in _UNKNOWN_DOC_TYPES}


def route(
    classification: Classification,
    sensitivity: SensitivityBucket,
    *,
    gate_open: bool,
    conf_floor: float,
    redact_active: bool = False,
) -> tuple[GateDecision, str]:
    """Decide the egress route for one document and explain why.

    Args:
        classification: The Stage-2 classification (doc_type + confidence).
        sensitivity: The resolved sensitivity bucket for the document.
        gate_open: Operator master switch; when ``False`` nothing is sent to the LLM.
        conf_floor: Minimum classifier confidence required to trust the doc_type label.
        redact_active: Whether PII redaction is wired up and active for this run. When true,
            sensitive documents may be redacted and sent rather than blocked outright.

    Returns:
        A ``(GateDecision, rationale)`` tuple. ``rationale`` is a short human-readable string
        describing the deciding rule (suitable for ``GateResult.rationale`` / audit logs).
    """
    doc_type = classification.doc_type
    confidence = classification.confidence
    is_low = sensitivity == SensitivityBucket.low
    is_medium = sensitivity == SensitivityBucket.medium
    is_high_or_critical = sensitivity in (SensitivityBucket.high, SensitivityBucket.critical)
    low_confidence = _is_unknown_doc_type(doc_type) or confidence < conf_floor

    # Rule 1: unknown / low-confidence classification on anything sensitive stays deterministic.
    # We will not ship content we cannot confidently classify unless it is plainly LOW sensitivity.
    if low_confidence and not is_low:
        return (
            GateDecision.deterministic_only,
            f"low-confidence classification (doc_type={doc_type!r}, "
            f"confidence={confidence:.2f} < floor={conf_floor:.2f}) and sensitivity="
            f"{sensitivity.value}; keeping deterministic-only",
        )

    # Rule 2: HIGH or CRITICAL sensitivity. Block egress unless redaction can sanitise first.
    if is_high_or_critical:
        if redact_active:
            return (
                GateDecision.redact_then_send,
                f"{sensitivity.value} sensitivity with redaction active; redact then send",
            )
        return (
            GateDecision.deterministic_only,
            f"{sensitivity.value} sensitivity and redaction inactive; deterministic-only",
        )

    # Rule 3: MEDIUM sensitivity. Redact-and-send only when redaction is active.
    if is_medium:
        if redact_active:
            return (
                GateDecision.redact_then_send,
                "MEDIUM sensitivity with redaction active; redact then send",
            )
        return (
            GateDecision.deterministic_only,
            "MEDIUM sensitivity and redaction inactive; deterministic-only",
        )

    # Rule 4: LOW sensitivity. Send to the LLM only when confident AND the gate is open.
    if is_low:
        if low_confidence:
            return (
                GateDecision.deterministic_only,
                f"LOW sensitivity but low-confidence classification "
                f"(confidence={confidence:.2f} < floor={conf_floor:.2f}); deterministic-only",
            )
        if not gate_open:
            return (
                GateDecision.deterministic_only,
                "LOW sensitivity and confident, but gate is closed; deterministic-only",
            )
        return (
            GateDecision.send_to_llm,
            f"LOW sensitivity, confident classification (doc_type={doc_type!r}, "
            f"confidence={confidence:.2f} >= floor={conf_floor:.2f}), gate open; send to LLM",
        )

    # Rule 5: any unexpected state -> fail safe.
    return (
        GateDecision.deterministic_only,
        f"unhandled state (sensitivity={sensitivity.value}); failing safe to deterministic-only",
    )
