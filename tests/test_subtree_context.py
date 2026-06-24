"""Unit tests for Anthropic-style contextual prefixes (di.subtree.context).

Runs fully offline against the in-process retrieval stub (forced via tests/conftest). Covers:
content-bearing chunk nodes get a non-empty ``context_prefix``; structural document/section roots
are skipped; concurrency is bounded; a failing gateway call leaves a node's prefix at ``None``
without aborting the batch.
"""
from __future__ import annotations

from di.models import KNode, NodeType
from di.retrieval_client import StubRetrievalClient
from di.subtree.context import add_context_prefixes


def _chunk(seq: int, content: str) -> KNode:
    return KNode(
        id=f"chunk-{seq}",
        client_id="c1",
        doc_id="d1",
        version_id="v1",
        path=f"doc.chunk{seq}",
        node_type=NodeType.chunk,
        seq=seq,
        content=content,
    )


def _document_root() -> KNode:
    return KNode(
        id="doc-root",
        client_id="c1",
        doc_id="d1",
        version_id="v1",
        path="doc",
        node_type=NodeType.document,
        title="Sample Document",
    )


_DOC_TEXT = (
    "ACME 2023 Annual Report.\n\n"
    "Section 1 covers revenue, which grew 12% year over year.\n\n"
    "Section 2 covers headcount and hiring across regions."
)


async def test_chunk_nodes_get_prefix_document_root_skipped():
    """Two chunk nodes get a non-empty context_prefix; the document root is skipped."""
    nodes = [
        _document_root(),
        _chunk(0, "Revenue grew 12% year over year to $4.2M."),
        _chunk(1, "Headcount rose from 40 to 55 employees."),
    ]
    stub = StubRetrievalClient()

    result = await add_context_prefixes(nodes, full_doc_text=_DOC_TEXT, client=stub)

    # Same list object returned, mutated in place.
    assert result is nodes

    root = next(n for n in result if n.node_type == NodeType.document)
    chunks = [n for n in result if n.node_type == NodeType.chunk]

    assert root.context_prefix is None
    assert len(chunks) == 2
    for ch in chunks:
        assert ch.context_prefix is not None
        assert ch.context_prefix.strip() != ""
        # Stub echoes the task tag, confirming the 'contextual' task was used.
        assert "stub:contextual" in ch.context_prefix


async def test_default_client_offline_stub():
    """With no client argument the default gateway (the stub, per conftest) is used offline."""
    nodes = [_chunk(0, "A standalone clause with no surrounding context.")]
    result = await add_context_prefixes(nodes, full_doc_text=_DOC_TEXT)
    assert result[0].context_prefix is not None


async def test_doc_text_is_capped_in_prompt():
    """A very long document is truncated before being sent to the gateway."""
    captured: list[list[dict[str, str]]] = []

    class CapturingStub(StubRetrievalClient):
        async def llm_complete(self, messages, **kwargs):  # type: ignore[override]
            captured.append(messages)
            return await super().llm_complete(messages, **kwargs)

    huge_doc = "word " * 50_000  # ~250k chars, far above the ~12k cap
    nodes = [_chunk(0, "An excerpt to situate.")]

    await add_context_prefixes(nodes, full_doc_text=huge_doc, client=CapturingStub())

    assert len(captured) == 1
    user_msg = captured[0][-1]["content"]
    # The prompt scaffolding adds a little, but the embedded doc text must be bounded.
    assert len(user_msg) < 13_000


async def test_node_with_no_content_left_none():
    """A content-type node with empty content is skipped (prefix stays None)."""
    blank = KNode(
        id="blank",
        client_id="c1",
        doc_id="d1",
        version_id="v1",
        path="doc.blank",
        node_type=NodeType.chunk,
        content="   ",
    )
    result = await add_context_prefixes([blank], full_doc_text=_DOC_TEXT, client=StubRetrievalClient())
    assert result[0].context_prefix is None


async def test_gateway_error_leaves_prefix_none_without_aborting():
    """A failing gateway call is swallowed: that node stays None, others still get a prefix."""

    class FlakyStub(StubRetrievalClient):
        async def llm_complete(self, messages, **kwargs):  # type: ignore[override]
            user = messages[-1]["content"]
            if "boom" in user:
                raise RuntimeError("simulated gateway failure")
            return await super().llm_complete(messages, **kwargs)

    nodes = [
        _chunk(0, "this one is fine"),
        _chunk(1, "this one says boom and explodes"),
    ]
    result = await add_context_prefixes(nodes, full_doc_text=_DOC_TEXT, client=FlakyStub())

    ok = next(n for n in result if "fine" in (n.content or ""))
    failed = next(n for n in result if "boom" in (n.content or ""))
    assert ok.context_prefix is not None
    assert failed.context_prefix is None


async def test_fact_node_synthesises_content():
    """A fact node with no `content` but an attribute_key/value still gets a prefix."""
    fact = KNode(
        id="fact-1",
        client_id="c1",
        doc_id="d1",
        version_id="v1",
        path="doc.fact1",
        node_type=NodeType.fact,
        attribute_key="identity.full_name",
        value_text="Jane Roe",
    )
    result = await add_context_prefixes([fact], full_doc_text=_DOC_TEXT, client=StubRetrievalClient())
    assert result[0].context_prefix is not None


async def test_empty_node_list_returns_empty():
    result = await add_context_prefixes([], full_doc_text=_DOC_TEXT, client=StubRetrievalClient())
    assert result == []
