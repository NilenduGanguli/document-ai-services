"""Per-document knowledge subtree assembly — pure, in-memory, DB-free.

Takes the artefacts produced upstream (classification, OCR, deterministic/LLM facts) and assembles
a tree of :class:`~di.models.KNode` objects rooted at one ``document`` node. The shape is::

    document (root, path = base_path)
    ├── section  (one per OCR page, or a single 'body' section from ocr.text)
    │   ├── chunk
    │   └── chunk
    ├── section ...
    └── facts (section)
        ├── fact  (one per ExtractedField)
        └── fact ...

Every node gets a PostgreSQL ``ltree`` ``path`` built by appending a sanitized label to its
parent's path (``[^A-Za-z0-9_]`` collapsed to ``_``, lowercased), a real ``uuid4`` ``id`` so that
``parent_id`` linkage is correct, a sibling ``seq``, and a ``depth`` equal to the number of
dot-separated labels in its path (ltree ``nlevel``).

Azure Vision Read OCR is flat (lines, not a logical hierarchy) so "sections" are synthesised by
grouping lines per page. Chunking is delegated to :func:`di.subtree.chunk.chunk_text` using the
configured token budgets.

Pure module: imports only :mod:`di` models/config/chunker and the stdlib. No network, no DB.
"""
from __future__ import annotations

import re
import uuid

from di.config import get_settings
from di.models import (
    Classification,
    ExtractedField,
    KNode,
    NodeType,
    OcrResult,
    Provenance,
    SensitivityBucket,
)
from di.subtree.chunk import chunk_text

__all__ = ["build_subtree", "nlevel", "sanitize_label"]

# Any character outside the ltree-safe alphabet becomes an underscore; runs collapse to one.
_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9_]+")
_LABEL_TRIM = re.compile(r"_+")


def _new_id() -> str:
    """Generate a fresh node id (uuid4 hex-with-dashes). Indirected so tests can monkeypatch."""
    return str(uuid.uuid4())


def sanitize_label(label: str) -> str:
    """Sanitize ``label`` into a single ltree-safe path label.

    Lowercases, replaces every run of non ``[A-Za-z0-9_]`` characters with ``_``, collapses
    repeated underscores, and strips leading/trailing underscores. An empty / all-unsafe label
    falls back to ``"x"`` so the result is always a valid, non-empty ltree label.
    """
    collapsed = _LABEL_UNSAFE.sub("_", label.strip().lower())
    collapsed = _LABEL_TRIM.sub("_", collapsed).strip("_")
    return collapsed or "x"


def nlevel(path: str) -> int:
    """Return the ltree ``nlevel`` of ``path`` — the count of dot-separated labels."""
    if not path:
        return 0
    return len([label for label in path.split(".") if label])


def build_subtree(
    *,
    client_id: str,
    doc_id: str,
    version_id: str,
    classification: Classification,
    ocr: OcrResult,
    facts: list[ExtractedField],
    base_path: str,
    doc_sensitivity: SensitivityBucket = SensitivityBucket.low,
) -> list[KNode]:
    """Assemble the in-memory knowledge subtree for one document version.

    Args:
        client_id: Owning client/tenant id (stamped on every node).
        doc_id: Document id this subtree belongs to.
        version_id: Document version id this subtree belongs to.
        classification: Doc-type classification (drives the root title).
        ocr: Flat OCR result (Azure Vision Read). Lines are grouped per page into sections; when
            there are no lines a single ``body`` section is derived from ``ocr.text``.
        facts: Extracted fields; one ``fact`` node is created per field under a ``facts`` section.
        base_path: The (already-sanitized) ltree path for the root document node, e.g.
            ``"client_42.doctype_us_passport.v1"``. Child labels are appended to it.
        doc_sensitivity: The gate's document-level sensitivity, inherited by the structural and
            content nodes (document/section/chunk). Their raw text quotes the very PII the fact
            nodes redact — a chunk reading "Passport No: 123456789" under a masked fact node is
            not masked at all — so they must carry the document's sensitivity, not the default.
            Fact nodes keep their own, more specific per-field sensitivity.

    Returns:
        A flat list of :class:`~di.models.KNode`, root first, then sections/chunks, then facts.
        Every node has a real ``id``; ``parent_id`` references its parent's ``id``; ``path`` is
        child-prefixed by its parent's ``path``; ``depth`` equals ``nlevel(path)``.
    """
    settings = get_settings()
    max_tokens = settings.chunk_max_tokens
    overlap_tokens = settings.chunk_overlap_tokens

    nodes: list[KNode] = []

    def add(node: KNode) -> KNode:
        nodes.append(node)
        return node

    root = add(
        KNode(
            id=_new_id(),
            client_id=client_id,
            doc_id=doc_id,
            version_id=version_id,
            parent_id=None,
            path=base_path,
            node_type=NodeType.document,
            seq=0,
            depth=nlevel(base_path),
            title=classification.doc_type or None,
            confidence=classification.confidence,
            sensitivity=doc_sensitivity,
        )
    )

    # --- Sections + chunks from OCR ------------------------------------------------------------
    section_seq = 0
    for page, page_text in _sections_from_ocr(ocr):
        section = add(
            KNode(
                id=_new_id(),
                client_id=client_id,
                doc_id=doc_id,
                version_id=version_id,
                parent_id=root.id,
                path=f"{root.path}.{sanitize_label(f's{section_seq}')}",
                node_type=NodeType.section,
                seq=section_seq,
                depth=nlevel(root.path) + 1,
                title=f"page {page}" if page is not None else "body",
                content=page_text or None,
                sensitivity=doc_sensitivity,   # its text quotes the document's PII verbatim
                provenance=_provenance(doc_id, version_id, page),
            )
        )
        section_seq += 1

        chunk_seq = 0
        for chunk in chunk_text(
            page_text, max_tokens=max_tokens, overlap_tokens=overlap_tokens
        ):
            add(
                KNode(
                    id=_new_id(),
                    client_id=client_id,
                    doc_id=doc_id,
                    version_id=version_id,
                    parent_id=section.id,
                    path=f"{section.path}.{sanitize_label(f'c{chunk_seq}')}",
                    node_type=NodeType.chunk,
                    seq=chunk_seq,
                    depth=nlevel(section.path) + 1,
                    content=chunk,
                    token_count=_estimate_tokens(chunk),
                    sensitivity=doc_sensitivity,   # raw OCR text of a sensitive document
                    provenance=_provenance(doc_id, version_id, page),
                )
            )
            chunk_seq += 1

    # --- Facts ---------------------------------------------------------------------------------
    if facts:
        facts_section = add(
            KNode(
                id=_new_id(),
                client_id=client_id,
                doc_id=doc_id,
                version_id=version_id,
                parent_id=root.id,
                path=f"{root.path}.{sanitize_label(f's{section_seq}')}",
                node_type=NodeType.section,
                seq=section_seq,
                depth=nlevel(root.path) + 1,
                title="facts",
            )
        )
        for fact_seq, field in enumerate(facts):
            add(
                KNode(
                    id=_new_id(),
                    client_id=client_id,
                    doc_id=doc_id,
                    version_id=version_id,
                    parent_id=facts_section.id,
                    path=f"{facts_section.path}.{sanitize_label(f'f{fact_seq}')}",
                    node_type=NodeType.fact,
                    seq=fact_seq,
                    depth=nlevel(facts_section.path) + 1,
                    title=field.attribute_key,
                    attribute_key=field.attribute_key,
                    value_text=field.value,
                    value_date=field.value_date,
                    value_num=field.value_num,
                    verification_status=field.verification_status,
                    confidence=field.confidence,
                    sensitivity=field.sensitivity,
                    provenance=_provenance_for_field(doc_id, version_id, field),
                )
            )

    return nodes


def _sections_from_ocr(ocr: OcrResult) -> list[tuple[int | None, str]]:
    """Group OCR into ``(page, text)`` sections.

    Lines are grouped by page (in ascending page order, then source order within a page). When the
    OCR has no lines a single ``(None, ocr.text)`` body section is returned so the document still
    gets chunked from its full text. Pages/bodies with no usable text are dropped.
    """
    if not ocr.lines:
        text = (ocr.text or "").strip()
        return [(None, text)] if text else []

    by_page: dict[int, list[str]] = {}
    for line in ocr.lines:
        by_page.setdefault(line.page, []).append(line.text)

    sections: list[tuple[int | None, str]] = []
    for page in sorted(by_page):
        text = "\n".join(t for t in by_page[page] if t).strip()
        if text:
            sections.append((page, text))
    return sections


def _provenance(doc_id: str, version_id: str, page: int | None) -> Provenance:
    return Provenance(document_id=doc_id, version_id=version_id, page=page)


def _provenance_for_field(
    doc_id: str, version_id: str, field: ExtractedField
) -> Provenance:
    """Provenance for a fact node, carrying the field's bbox/page and extractor when present."""
    page = field.bbox.page if field.bbox is not None else None
    return Provenance(
        document_id=doc_id,
        version_id=version_id,
        page=page,
        bbox=field.bbox,
        extractor=field.source.value,
    )


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate mirroring the chunker's ``len // 4`` heuristic."""
    stripped = text.strip()
    return len(stripped) // 4 if stripped else 0
