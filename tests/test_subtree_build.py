"""Unit tests for di.subtree.build (pure, DB-free, offline)."""
from __future__ import annotations

from datetime import date

from di.models import (
    BBox,
    Classification,
    ExtractedField,
    ExtractionSource,
    NodeType,
    OcrLine,
    OcrResult,
    SensitivityBucket,
    VerificationStatus,
)
from di.subtree.build import build_subtree, nlevel, sanitize_label

BASE_PATH = "client_42.doctype_us_passport.v1"


def _ocr_two_lines_one_page() -> OcrResult:
    return OcrResult(
        engine="azure-vision-read",
        pages=1,
        text="JOHN SMITH\nDATE OF BIRTH 1990-01-02",
        lines=[
            OcrLine(text="JOHN SMITH", page=1, bbox=BBox(page=1, x0=0, y0=0, x1=1, y1=1)),
            OcrLine(
                text="DATE OF BIRTH 1990-01-02",
                page=1,
                bbox=BBox(page=1, x0=0, y0=1, x1=1, y1=2),
            ),
        ],
    )


def _two_facts() -> list[ExtractedField]:
    return [
        ExtractedField(
            attribute_key="identity.full_name",
            value="JOHN SMITH",
            source=ExtractionSource.anchor,
            confidence=0.9,
            sensitivity=SensitivityBucket.medium,
            verification_status=VerificationStatus.llm_unverified,
            bbox=BBox(page=1, x0=0, y0=0, x1=1, y1=1),
        ),
        ExtractedField(
            attribute_key="identity.date_of_birth",
            value="1990-01-02",
            value_date=date(1990, 1, 2),
            source=ExtractionSource.mrz,
            confidence=0.99,
            sensitivity=SensitivityBucket.high,
            verification_status=VerificationStatus.checksum_verified,
            bbox=BBox(page=1, x0=0, y0=1, x1=1, y1=2),
        ),
    ]


def _build() -> list:
    return build_subtree(
        client_id="client-42",
        doc_id="doc-1",
        version_id="ver-1",
        classification=Classification(doc_type="US_PASSPORT", confidence=0.88),
        ocr=_ocr_two_lines_one_page(),
        facts=_two_facts(),
        base_path=BASE_PATH,
    )


def test_sanitize_label_collapses_and_lowercases() -> None:
    assert sanitize_label("Page 2!!") == "page_2"
    assert sanitize_label("  --A__B-- ") == "a_b"
    assert sanitize_label("****") == "x"
    assert sanitize_label("s0") == "s0"


def test_nlevel_counts_dot_segments() -> None:
    assert nlevel("a.b.c") == 3
    assert nlevel(BASE_PATH) == 3
    assert nlevel("") == 0


def test_root_is_first_and_a_document() -> None:
    nodes = _build()
    root = nodes[0]
    assert root.node_type == NodeType.document
    assert root.path == BASE_PATH
    assert root.depth == nlevel(BASE_PATH)
    assert root.parent_id is None
    assert root.title == "US_PASSPORT"
    assert root.id is not None


def test_counts_one_section_one_chunk_two_facts() -> None:
    nodes = _build()
    docs = [n for n in nodes if n.node_type == NodeType.document]
    sections = [n for n in nodes if n.node_type == NodeType.section]
    chunks = [n for n in nodes if n.node_type == NodeType.chunk]
    facts = [n for n in nodes if n.node_type == NodeType.fact]

    assert len(docs) == 1
    # one page section + one facts section
    assert len(sections) >= 1
    assert len(chunks) >= 1
    assert len(facts) == 2


def test_fact_nodes_carry_field_data() -> None:
    nodes = _build()
    facts = {n.attribute_key: n for n in nodes if n.node_type == NodeType.fact}

    dob = facts["identity.date_of_birth"]
    assert dob.value_text == "1990-01-02"
    assert dob.value_date == date(1990, 1, 2)
    assert dob.verification_status == VerificationStatus.checksum_verified
    assert dob.confidence == 0.99
    assert dob.sensitivity == SensitivityBucket.high
    assert dob.provenance is not None
    assert dob.provenance.page == 1
    assert dob.provenance.bbox is not None
    assert dob.provenance.extractor == "mrz"


def test_paths_are_child_prefixed_and_parent_ids_resolve() -> None:
    nodes = _build()
    by_id = {n.id: n for n in nodes}
    root = nodes[0]

    for node in nodes:
        # depth always matches the ltree level of the path.
        assert node.depth == nlevel(node.path)
        if node.parent_id is None:
            assert node is root
            continue
        parent = by_id.get(node.parent_id)
        # parent_id must resolve to a real node in the returned list.
        assert parent is not None
        # child path must be prefixed by its parent's path + a single appended label.
        assert node.path.startswith(parent.path + ".")
        assert node.path.count(".") == parent.path.count(".") + 1
        # child is exactly one level deeper than its parent.
        assert node.depth == parent.depth + 1


def test_chunk_provenance_carries_page() -> None:
    nodes = _build()
    chunks = [n for n in nodes if n.node_type == NodeType.chunk]
    assert chunks
    for chunk in chunks:
        assert chunk.provenance is not None
        assert chunk.provenance.page == 1
        assert chunk.content
        assert chunk.token_count is not None


def test_no_lines_falls_back_to_body_section() -> None:
    ocr = OcrResult(engine="azure-vision-read", pages=1, text="Some body text here.", lines=[])
    nodes = build_subtree(
        client_id="c",
        doc_id="d",
        version_id="v",
        classification=Classification(doc_type="UTILITY_BILL"),
        ocr=ocr,
        facts=[],
        base_path="client_c.doctype_utility_bill.v1",
    )
    sections = [n for n in nodes if n.node_type == NodeType.section]
    assert len(sections) == 1
    assert sections[0].title == "body"
    # no facts -> no facts section, no fact nodes.
    assert not [n for n in nodes if n.node_type == NodeType.fact]


def test_seq_orders_siblings() -> None:
    nodes = _build()
    sections = [n for n in nodes if n.node_type == NodeType.section]
    # sibling seqs are distinct and start at 0.
    seqs = sorted(s.seq for s in sections)
    assert seqs == list(range(len(sections)))
