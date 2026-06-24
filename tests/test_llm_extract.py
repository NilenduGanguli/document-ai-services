"""Unit tests for the LLM extraction path (di.extract.llm_extract).

``extract_json`` is exercised on fenced, bare-braced, and garbage inputs. ``classify_and_extract``
is driven both by the offline :class:`~di.retrieval_client.StubRetrievalClient` (which returns
``{"stub": true}`` for JSON completions) and by small in-memory fake clients that emit realistic
classification / attribute payloads — all fully offline, none touching the network or DB.
"""
from __future__ import annotations

from typing import Any

import pytest

from di.extract.llm_extract import (
    MAX_OCR_CHARS,
    UNKNOWN_DOC_TYPE,
    classify_and_extract,
    extract_json,
)
from di.models import Classification, ExtractedField, ExtractionSource, VerificationStatus
from di.retrieval_client import StubRetrievalClient


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------
def test_extract_json_fenced_with_lang_tag() -> None:
    out = extract_json('```json\n{"doc_type": "US_W2", "confidence": 0.9}\n```')
    assert out == {"doc_type": "US_W2", "confidence": 0.9}


def test_extract_json_plain_fence() -> None:
    out = extract_json('```\n{"a": 1}\n```')
    assert out == {"a": 1}


def test_extract_json_bare_object() -> None:
    assert extract_json('{"k": "v"}') == {"k": "v"}


def test_extract_json_object_embedded_in_prose() -> None:
    """A brace-balanced scan recovers an object surrounded by prose."""
    out = extract_json('Sure! Here you go: {"doc_type": "MX_CURP", "x": {"nested": true}} -- done.')
    assert out == {"doc_type": "MX_CURP", "x": {"nested": True}}


def test_extract_json_ignores_braces_inside_strings() -> None:
    out = extract_json('{"text": "a } b { c", "ok": true}')
    assert out == {"text": "a } b { c", "ok": True}


def test_extract_json_garbage_returns_none() -> None:
    assert extract_json("not json at all") is None
    assert extract_json("") is None
    assert extract_json("{ unbalanced") is None


def test_extract_json_non_object_returns_none() -> None:
    """A valid JSON array (not an object) is rejected."""
    assert extract_json("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# Fake gateway clients
# ---------------------------------------------------------------------------
class _ScriptedClient:
    """Returns queued completion strings in order; records the messages it was called with."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def llm_complete(
        self,
        messages: list[dict[str, str]],
        *,
        task: str = "final_gen",
        temperature: float = 0.0,
        max_tokens: int = 1024,
        response_format: str = "text",
    ) -> tuple[str, dict[str, int]]:
        self.calls.append(messages)
        text = self._responses.pop(0) if self._responses else "{}"
        return text, {"prompt": 0, "completion": 0, "total": 0}


class _RaisingClient:
    """Always raises from llm_complete to exercise the degrade-to-empty path."""

    async def llm_complete(self, *args: Any, **kwargs: Any) -> tuple[str, dict[str, int]]:
        raise RuntimeError("gateway down")


# ---------------------------------------------------------------------------
# classify_and_extract
# ---------------------------------------------------------------------------
async def test_classify_and_extract_with_offline_stub() -> None:
    """The shipped stub returns {"stub": true} -> UNKNOWN classification, empty fields, no raise."""
    client = StubRetrievalClient()
    classification, fields = await classify_and_extract("some ocr text", client=client)

    assert isinstance(classification, Classification)
    assert classification.doc_type == UNKNOWN_DOC_TYPE
    assert isinstance(fields, list)
    assert fields == []


async def test_classify_and_extract_happy_path() -> None:
    client = _ScriptedClient(
        [
            '```json\n{"doc_type": "US_W2", "doc_category": "tax", '
            '"jurisdiction": "US", "confidence": 0.92, "signals": ["W-2"]}\n```',
            '{"attributes": {"identity.full_name": "Jane Roe", "tax.wages": 54000}, '
            '"confidence": 0.8}',
        ]
    )
    classification, fields = await classify_and_extract("Wage and Tax Statement", client=client)

    assert classification.doc_type == "US_W2"
    assert classification.doc_category == "tax"
    assert classification.jurisdiction == "US"
    assert classification.confidence == pytest.approx(0.92)
    assert classification.signals == ["W-2"]

    assert len(fields) == 2
    by_key = {f.attribute_key: f for f in fields}
    assert set(by_key) == {"identity.full_name", "tax.wages"}
    name = by_key["identity.full_name"]
    assert isinstance(name, ExtractedField)
    assert name.value == "Jane Roe"
    assert name.source is ExtractionSource.llm
    assert name.verification_status is VerificationStatus.llm_unverified
    assert name.confidence == pytest.approx(0.8)
    # Non-string values are stringified.
    assert by_key["tax.wages"].value == "54000"

    # Two round-trips: classify, then extract.
    assert len(client.calls) == 2


async def test_classify_and_extract_flat_attribute_shape() -> None:
    """An extract payload mapping keys directly to values (no "attributes" wrapper) works."""
    client = _ScriptedClient(
        [
            '{"doc_type": "MX_CURP", "confidence": 0.7}',
            '{"identity.curp": "ROEJ900101HDFXXX01", "confidence": 0.6}',
        ]
    )
    classification, fields = await classify_and_extract("CURP doc", client=client)

    assert classification.doc_type == "MX_CURP"
    assert len(fields) == 1
    assert fields[0].attribute_key == "identity.curp"
    assert fields[0].value == "ROEJ900101HDFXXX01"
    # The reserved "confidence" key is not turned into a field.
    assert fields[0].confidence == pytest.approx(0.6)


async def test_classify_and_extract_default_confidence_when_unscored() -> None:
    client = _ScriptedClient(
        [
            '{"doc_type": "PASSPORT"}',
            '{"attributes": {"identity.surname": "Doe"}}',
        ]
    )
    classification, fields = await classify_and_extract("passport", client=client)

    assert classification.doc_type == "PASSPORT"
    # No model confidence supplied -> default 0.5 on the field.
    assert fields[0].confidence == pytest.approx(0.5)


async def test_classify_unknown_skips_extraction() -> None:
    """When classification is UNKNOWN, the extract round-trip is not attempted."""
    client = _ScriptedClient(['{"not_a_doc_type": 1}'])
    classification, fields = await classify_and_extract("???", client=client)

    assert classification.doc_type == UNKNOWN_DOC_TYPE
    assert fields == []
    assert len(client.calls) == 1  # only the classify call happened


async def test_classify_and_extract_tolerates_gateway_errors() -> None:
    classification, fields = await classify_and_extract("anything", client=_RaisingClient())
    assert classification.doc_type == UNKNOWN_DOC_TYPE
    assert fields == []


async def test_doc_type_hint_is_passed_to_prompt() -> None:
    client = _ScriptedClient(['{"doc_type": "CA_T4", "confidence": 0.5}', '{"attributes": {}}'])
    await classify_and_extract("statement", client=client, doc_type_hint="CA_T4")

    classify_user_msg = client.calls[0][-1]["content"]
    assert "CA_T4" in classify_user_msg


async def test_ocr_text_is_capped() -> None:
    """Oversized OCR text is truncated before being placed in the prompt."""
    big = "X" * (MAX_OCR_CHARS + 5000)
    client = _ScriptedClient(['{"doc_type": "BIG", "confidence": 0.5}', '{"attributes": {}}'])
    await classify_and_extract(big, client=client)

    classify_user_msg = client.calls[0][-1]["content"]
    assert classify_user_msg.count("X") == MAX_OCR_CHARS
