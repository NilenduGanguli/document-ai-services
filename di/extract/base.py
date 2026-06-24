"""Shared extraction contracts used by both the deterministic and LLM paths.

A ``DeterministicExtractor`` turns an OCR dump (text + optional line geometry) for a known
``doc_type`` into a list of :class:`~di.models.ExtractedField`. Implementations live in
``di/extract/deterministic/`` (one module per jurisdiction) and register themselves in the
registry so the pipeline can dispatch by ``doc_type``.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from di.models import ExtractedField, OcrLine


class ExtractionInput:
    """Everything an extractor needs. Cheap container (not a pydantic model on the hot path)."""

    __slots__ = ("doc_type", "text", "lines", "lang")

    def __init__(
        self,
        doc_type: str,
        text: str,
        lines: list[OcrLine] | None = None,
        lang: str = "en",
    ) -> None:
        self.doc_type = doc_type
        self.text = text
        self.lines = lines or []
        self.lang = lang


@runtime_checkable
class DeterministicExtractor(Protocol):
    """Pure, offline extractor for a set of fixed-format document types."""

    #: doc_type codes this extractor handles (e.g. {"PASAPORTE", "PASSPORT"})
    handles: frozenset[str]

    def extract(self, inp: ExtractionInput) -> list[ExtractedField]:
        ...


# Registry: doc_type -> extractor instance. Populated by deterministic submodules at import.
_REGISTRY: dict[str, DeterministicExtractor] = {}


def register(extractor: DeterministicExtractor) -> DeterministicExtractor:
    for doc_type in extractor.handles:
        _REGISTRY[doc_type] = extractor
    return extractor


def get_extractor(doc_type: str) -> DeterministicExtractor | None:
    return _REGISTRY.get(doc_type)


def registered_doc_types() -> frozenset[str]:
    return frozenset(_REGISTRY)
