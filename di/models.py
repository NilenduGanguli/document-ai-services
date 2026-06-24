"""Domain models and enums — the contracts shared across every module.

These pydantic models define the shapes that flow through the pipeline and the API.
Keep them dependency-free (no DB / no network) so any module can import them.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class NodeType(StrEnum):
    document = "document"
    section = "section"
    chunk = "chunk"
    table = "table"
    figure = "figure"
    fact = "fact"
    summary = "summary"


class RepType(StrEnum):
    hypothetical_q = "hypothetical_q"
    proposition = "proposition"
    summary = "summary"
    alt_phrasing = "alt_phrasing"
    synonym_expansion = "synonym_expansion"
    table_desc = "table_desc"
    figure_desc = "figure_desc"
    keyword_set = "keyword_set"
    translation = "translation"


class VerificationStatus(StrEnum):
    checksum_verified = "checksum_verified"   # deterministic ID passed its checksum
    gov_verified = "gov_verified"             # confirmed against a government endpoint (e.g. SAT)
    llm_unverified = "llm_unverified"         # extracted by the LLM, not independently verified
    unverified = "unverified"


class SensitivityBucket(StrEnum):
    low = "LOW"
    medium = "MEDIUM"
    high = "HIGH"
    critical = "CRITICAL"


class GateDecision(StrEnum):
    send_to_llm = "SEND_TO_LLM"
    redact_then_send = "REDACT_THEN_SEND"
    deterministic_only = "DETERMINISTIC_ONLY"


class ExtractionSource(StrEnum):
    mrz = "mrz"
    anchor = "anchor"
    positional = "positional"
    regex_sweep = "regex_sweep"
    llm = "llm"
    gov = "gov"


# ---------------------------------------------------------------------------
# Provenance & fields
# ---------------------------------------------------------------------------
class BBox(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class Provenance(BaseModel):
    document_id: str | None = None
    version_id: str | None = None
    page: int | None = None
    bbox: BBox | None = None
    char_span: tuple[int, int] | None = None
    extractor: str | None = None
    model: str | None = None
    extracted_at: datetime | None = None


class ExtractedField(BaseModel):
    """One extracted attribute. Shared by the deterministic and LLM paths."""
    attribute_key: str                              # canonical, e.g. "identity.date_of_birth"
    value: str | None = None
    value_date: date | None = None
    value_num: float | None = None
    raw_ocr: str | None = None
    source: ExtractionSource = ExtractionSource.llm
    checksum_ok: bool | None = None
    verification_status: VerificationStatus = VerificationStatus.unverified
    confidence: float = 0.0
    sensitivity: SensitivityBucket = SensitivityBucket.low
    bbox: BBox | None = None


# ---------------------------------------------------------------------------
# OCR / language
# ---------------------------------------------------------------------------
class OcrLine(BaseModel):
    text: str
    page: int
    bbox: BBox | None = None
    confidence: float | None = None


class OcrResult(BaseModel):
    engine: str
    pages: int
    text: str
    lines: list[OcrLine] = Field(default_factory=list)


class LangSpan(BaseModel):
    start: int
    end: int
    lang: str


class LangProfile(BaseModel):
    dominant_lang: str
    is_bilingual: bool = False
    spans: list[LangSpan] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Classification / gate
# ---------------------------------------------------------------------------
class Classification(BaseModel):
    doc_type: str
    doc_category: str | None = None
    jurisdiction: str | None = None
    confidence: float = 0.0
    signals: list[str] = Field(default_factory=list)


class PiiEntity(BaseModel):
    entity_type: str
    start: int
    end: int
    score: float
    lang: str = "en"


class GateResult(BaseModel):
    classification: Classification
    lang_profile: LangProfile
    pii_entities: list[PiiEntity] = Field(default_factory=list)
    sensitivity: SensitivityBucket = SensitivityBucket.low
    decision: GateDecision = GateDecision.deterministic_only
    rationale: str = ""


# ---------------------------------------------------------------------------
# Knowledge subtree nodes / reps (in-memory shapes; DB rows mirror these)
# ---------------------------------------------------------------------------
class KNode(BaseModel):
    id: str | None = None
    client_id: str
    doc_id: str
    version_id: str
    parent_id: str | None = None
    path: str                                        # ltree label path
    node_type: NodeType
    seq: int = 0
    depth: int = 0
    title: str | None = None
    content: str | None = None
    context_prefix: str | None = None
    attribute_key: str | None = None                 # for fact nodes
    value_text: str | None = None
    value_date: date | None = None
    value_num: float | None = None
    verification_status: VerificationStatus = VerificationStatus.unverified
    confidence: float = 0.0
    sensitivity: SensitivityBucket = SensitivityBucket.low
    valid_from: date | None = None
    valid_to: date | None = None
    cross_refs: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    provenance: Provenance | None = None
    token_count: int | None = None
    embedding: list[float] | None = None             # transient; persisted to the vector column


class ARep(BaseModel):
    id: str | None = None
    knode_id: str
    client_id: str
    doc_id: str
    version_id: str
    path: str
    rep_type: RepType
    rep_lang: str = "en"
    rep_text: str
    gen_model: str | None = None
    embedding: list[float] | None = None


class ClientFact(BaseModel):
    client_id: str
    attribute_key: str
    resolved_value: str | None = None
    value_date: date | None = None
    value_num: float | None = None
    confidence: float = 0.0
    conflict: bool = False
    needs_review: bool = False
    source_fact_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Document / version
# ---------------------------------------------------------------------------
class DocumentMeta(BaseModel):
    id: str | None = None
    client_id: str
    document_name: str
    s3_uri: str | None = None
    sha256: str | None = None
    mime: str | None = None
    doc_type: str | None = None
    doc_category: str | None = None
    subject: str | None = None
    jurisdiction: str | None = None
    sensitivity_bucket: SensitivityBucket = SensitivityBucket.low
    gate_decision: GateDecision | None = None
    confidence: float = 0.0
    ocr_engine: str | None = None
    page_count: int | None = None


# ---------------------------------------------------------------------------
# SSE ingest events
# ---------------------------------------------------------------------------
class IngestEvent(BaseModel):
    stage: str
    status: str = "done"          # start | progress | done | error | skip
    detail: dict[str, Any] = Field(default_factory=dict)
