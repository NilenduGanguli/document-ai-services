"""LLM classification + adaptive attribute extraction via the retrieval gateway.

This is the *open* (LLM) extraction path, complementing the deterministic extractors in
``di/extract/deterministic/``. It does two gateway round-trips through
:func:`di.retrieval_client.get_retrieval_client`:

1. **Classify** — ask the model to name the ``doc_type`` (+ optional category / jurisdiction) for
   the OCR text, returning a :class:`~di.models.Classification`.
2. **Extract** — ask the model to *choose* and return the salient ``{key: value}`` attributes for
   the identified doc type, mapped to a list of :class:`~di.models.ExtractedField` (one per key).

Everything is fully tolerant: the gateway may return fenced JSON, bare JSON, or garbage. Malformed
or empty responses degrade to an ``UNKNOWN`` classification and an empty field list rather than
raising. In-memory only — no DB, no direct network (the gateway client owns transport).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from di.models import (
    Classification,
    ExtractedField,
    ExtractionSource,
    VerificationStatus,
)
from di.retrieval_client import get_retrieval_client

logger = logging.getLogger(__name__)

#: Returned when the model fails to identify a document type.
UNKNOWN_DOC_TYPE = "UNKNOWN"

#: Max OCR characters sent to the model (keeps prompts within budget).
MAX_OCR_CHARS = 6000

#: Fallback confidence when the model does not supply a numeric score.
DEFAULT_CONFIDENCE = 0.5

_CLASSIFY_SYSTEM = (
    "You are a document classification engine. Given the OCR text of a single document, identify "
    "its type. Respond with ONLY a JSON object with keys: doc_type (a short UPPER_SNAKE_CASE code), "
    "doc_category (optional broad category), jurisdiction (optional ISO country code), confidence "
    "(0..1 float), signals (optional list of short strings). No prose, no markdown."
)

_EXTRACT_SYSTEM = (
    "You are a document attribute extraction engine. Given the OCR text of a document and its "
    "identified type, choose the salient attributes a human reviewer would record and return them. "
    "Respond with ONLY a JSON object whose top-level key is \"attributes\", mapping attribute keys "
    "(short snake_case or dotted, e.g. \"identity.full_name\") to their string values. You MAY "
    'instead return a JSON object mapping keys directly to values. Optionally include a "confidence" '
    "(0..1 float). No prose, no markdown."
)


def extract_json(text: str) -> dict[str, Any] | None:
    """Tolerantly parse a JSON object out of a model completion.

    Strategy, in order:

    1. Strip a leading/trailing ```json (or plain ```) code fence, then ``json.loads``.
    2. Otherwise scan for the first brace-balanced ``{...}`` region and ``json.loads`` that.

    Returns the parsed ``dict`` on success, or ``None`` when nothing parseable is found (or the
    parsed value is not an object). Never raises.
    """
    if not text:
        return None

    stripped = text.strip()
    fenced = _strip_code_fence(stripped)
    if fenced is not None:
        parsed = _try_load_object(fenced)
        if parsed is not None:
            return parsed

    # Try the whole (de-fenced or raw) string directly.
    parsed = _try_load_object(stripped)
    if parsed is not None:
        return parsed

    # Fall back to a brace-balanced scan for the first JSON object.
    candidate = _first_balanced_object(stripped)
    if candidate is not None:
        return _try_load_object(candidate)

    return None


def _strip_code_fence(text: str) -> str | None:
    """If ``text`` is wrapped in a Markdown code fence, return its inner body, else ``None``."""
    if not text.startswith("```"):
        return None
    # Drop the opening fence line (``` or ```json) and the trailing fence.
    body = text[3:]
    newline = body.find("\n")
    if newline != -1:
        # Discard an optional language tag on the opening fence line (e.g. "json").
        body = body[newline + 1 :]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def _try_load_object(text: str) -> dict[str, Any] | None:
    """``json.loads`` ``text`` and return it only if it is a JSON object."""
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _first_balanced_object(text: str) -> str | None:
    """Return the first brace-balanced ``{...}`` substring, respecting JSON string literals."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _cap_text(text: str) -> str:
    """Trim OCR text to the model budget (whole-string slice; cheap and deterministic)."""
    if len(text) <= MAX_OCR_CHARS:
        return text
    return text[:MAX_OCR_CHARS]


def _coerce_confidence(value: Any, default: float = DEFAULT_CONFIDENCE) -> float:
    """Best-effort float in ``[0, 1]`` from a model-supplied score, else ``default``."""
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        return default
    if isinstance(value, (int, float)):
        score = float(value)
    elif isinstance(value, str):
        try:
            score = float(value.strip())
        except ValueError:
            return default
    else:
        return default
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def _classification_from_payload(payload: dict[str, Any] | None) -> Classification:
    """Build a :class:`Classification` from a parsed classify payload, defaulting to UNKNOWN."""
    if not payload:
        return Classification(doc_type=UNKNOWN_DOC_TYPE, confidence=0.0)

    raw_doc_type = payload.get("doc_type")
    doc_type = str(raw_doc_type).strip() if raw_doc_type else ""
    if not doc_type:
        return Classification(doc_type=UNKNOWN_DOC_TYPE, confidence=0.0)

    raw_signals = payload.get("signals")
    signals = [str(s) for s in raw_signals] if isinstance(raw_signals, list) else []

    doc_category = payload.get("doc_category")
    jurisdiction = payload.get("jurisdiction")
    return Classification(
        doc_type=doc_type,
        doc_category=str(doc_category) if doc_category else None,
        jurisdiction=str(jurisdiction) if jurisdiction else None,
        confidence=_coerce_confidence(payload.get("confidence"), default=0.0),
        signals=signals,
    )


def _attributes_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Pull the attribute mapping out of an extract payload.

    Accepts both ``{"attributes": {...}}`` and a flat ``{key: value}`` object. The reserved
    ``confidence`` key is never treated as an attribute.
    """
    if not payload:
        return {}
    attrs = payload.get("attributes")
    if isinstance(attrs, dict):
        return attrs
    # Flat shape: every key except the reserved score is an attribute.
    return {k: v for k, v in payload.items() if k != "confidence"}


def _value_to_str(value: Any) -> str | None:
    """Render a model attribute value as a string; lists/dicts are JSON-encoded."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _fields_from_payload(payload: dict[str, Any] | None) -> list[ExtractedField]:
    """Map a parsed extract payload to a list of LLM-sourced :class:`ExtractedField`."""
    attributes = _attributes_from_payload(payload)
    if not attributes:
        return []

    confidence = _coerce_confidence(payload.get("confidence") if payload else None)
    fields: list[ExtractedField] = []
    for key, raw_value in attributes.items():
        attribute_key = str(key).strip()
        if not attribute_key:
            continue
        fields.append(
            ExtractedField(
                attribute_key=attribute_key,
                value=_value_to_str(raw_value),
                source=ExtractionSource.llm,
                verification_status=VerificationStatus.llm_unverified,
                confidence=confidence,
            )
        )
    return fields


async def classify_and_extract(
    ocr_text: str,
    *,
    client: Any | None = None,
    doc_type_hint: str | None = None,
) -> tuple[Classification, list[ExtractedField]]:
    """Classify ``ocr_text`` and extract its salient attributes via the model gateway.

    Two ``llm_complete(task='final_gen', response_format='json')`` calls are made: one to classify
    the document, one to extract attributes for the identified type. The optional ``doc_type_hint``
    biases (but does not override) classification.

    Parameters
    ----------
    ocr_text:
        Raw OCR dump of the document. Capped to :data:`MAX_OCR_CHARS` before sending.
    client:
        A retrieval client (live or :class:`~di.retrieval_client.StubRetrievalClient`). Defaults to
        :func:`~di.retrieval_client.get_retrieval_client` so tests can run fully offline.
    doc_type_hint:
        Optional prior doc-type code (e.g. from the deterministic gate) passed to the classifier.

    Returns
    -------
    tuple[Classification, list[ExtractedField]]
        Always returns a pair. On any malformed/empty model output the result degrades to
        ``(Classification(doc_type="UNKNOWN", ...), [])`` — this function never raises on model I/O.
    """
    client = client or get_retrieval_client()
    capped = _cap_text(ocr_text or "")

    classification = await _classify(client, capped, doc_type_hint)
    if classification.doc_type == UNKNOWN_DOC_TYPE:
        return classification, []

    fields = await _extract_attributes(client, capped, classification.doc_type)
    return classification, fields


async def _classify(
    client: Any,
    capped_text: str,
    doc_type_hint: str | None,
) -> Classification:
    """Run the classification round-trip; degrade to UNKNOWN on any error."""
    hint = f'\n\nA prior guess at the type is "{doc_type_hint}" (verify it).' if doc_type_hint else ""
    user = f"OCR text of the document:\n\n{capped_text}{hint}"
    payload = await _complete_json(
        client,
        [
            {"role": "system", "content": _CLASSIFY_SYSTEM},
            {"role": "user", "content": user},
        ],
    )
    return _classification_from_payload(payload)


async def _extract_attributes(
    client: Any,
    capped_text: str,
    doc_type: str,
) -> list[ExtractedField]:
    """Run the attribute-extraction round-trip; degrade to ``[]`` on any error."""
    user = (
        f'The document type is "{doc_type}". Choose and return its salient attributes.\n\n'
        f"OCR text:\n\n{capped_text}"
    )
    payload = await _complete_json(
        client,
        [
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": user},
        ],
    )
    return _fields_from_payload(payload)


async def _complete_json(
    client: Any,
    messages: list[dict[str, str]],
) -> dict[str, Any] | None:
    """Call the gateway for a JSON completion and tolerantly parse it; ``None`` on any failure."""
    try:
        text, _usage = await client.llm_complete(
            messages,
            task="final_gen",
            response_format="json",
        )
    except Exception as e:  # noqa: BLE001 - model/transport issues must never break extraction
        logger.warning("llm_complete failed; degrading to empty result: %s", e)
        return None
    return extract_json(text)
