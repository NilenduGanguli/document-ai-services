"""Unit tests for the accessibility-representation generator (offline stub gateway)."""
from __future__ import annotations

from di.config import get_settings
from di.models import KNode, NodeType, RepType
from di.retrieval_client import StubRetrievalClient
from di.subtree.arep import generate_areps


def _chunk_node(**overrides: object) -> KNode:
    base: dict[str, object] = {
        "id": "node-1",
        "client_id": "client-1",
        "doc_id": "doc-1",
        "version_id": "ver-1",
        "path": "doc.sec.chunk0",
        "node_type": NodeType.chunk,
        "content": "The account holder is John Smith, born 1980-01-02 in Austin, Texas.",
    }
    base.update(overrides)
    return KNode(**base)


async def test_chunk_node_yields_base_reps_and_translation():
    client = StubRetrievalClient(get_settings())
    node = _chunk_node()

    reps = await generate_areps([node], client=client, languages=("en", "es"))

    assert reps, "expected at least one representation"
    # Every rep is bound to the source node and carries the node's coordinates.
    for rep in reps:
        assert rep.knode_id == node.id
        assert rep.client_id == node.client_id
        assert rep.doc_id == node.doc_id
        assert rep.version_id == node.version_id
        assert rep.path == node.path
        assert rep.rep_text
        assert rep.embedding is None  # embeddings are computed downstream, not here

    rep_types = {rep.rep_type for rep in reps}
    assert RepType.hypothetical_q in rep_types
    assert RepType.proposition in rep_types
    assert RepType.summary in rep_types

    # A translation rep into the *other* supported language (source is 'en' -> 'es').
    translations = [rep for rep in reps if rep.rep_type == RepType.translation]
    assert len(translations) == 1
    assert translations[0].rep_lang == "es"
    assert translations[0].rep_lang != "en"


async def test_table_node_includes_table_desc():
    client = StubRetrievalClient(get_settings())
    node = _chunk_node(
        id="tbl-1",
        node_type=NodeType.table,
        content="| Year | Income |\n| 2023 | 50000 |",
    )

    reps = await generate_areps([node], client=client)

    rep_types = {rep.rep_type for rep in reps}
    assert RepType.table_desc in rep_types
    assert RepType.figure_desc not in rep_types
    assert all(rep.knode_id == "tbl-1" for rep in reps)


async def test_rep_types_override_replaces_default_set():
    client = StubRetrievalClient(get_settings())
    node = _chunk_node()

    reps = await generate_areps(
        [node],
        client=client,
        rep_types=[RepType.summary],
    )

    # Override is exhaustive: only the requested rep type, and no auto translation rep.
    assert {rep.rep_type for rep in reps} == {RepType.summary}
    assert all(rep.rep_lang == "en" for rep in reps)


async def test_non_content_and_empty_nodes_are_skipped():
    client = StubRetrievalClient(get_settings())
    section = _chunk_node(id="sec-1", node_type=NodeType.section)
    empty_chunk = _chunk_node(id="empty-1", content="   ")

    reps = await generate_areps([section, empty_chunk], client=client)

    assert reps == []


async def test_empty_input_returns_empty_list():
    client = StubRetrievalClient(get_settings())
    assert await generate_areps([], client=client) == []
