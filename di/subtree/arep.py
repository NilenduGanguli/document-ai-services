"""Accessibility representations — the ``arep`` multi-vector layer.

Each content-bearing knowledge node (``chunk``/``table``/``figure``/``fact``) is expanded into a
small family of *alternative phrasings* of the same content, so that retrieval can match a query
against the representation that is closest in form to it — a hypothetical question, an atomic
proposition, a one-line summary, a paraphrase, a media description, or a cross-language
translation. These rows are the multi-vector layer: every :class:`~di.models.ARep` is embedded
separately downstream (this module deliberately does **not** compute embeddings).

Generation goes through the retrieval service's LLM gateway (``task='fast'``) via
:func:`di.retrieval_client.get_retrieval_client`. The work is fanned out with a bounded
:class:`asyncio.Semaphore` and every gateway call is individually guarded — a single failed
generation drops that one representation rather than sinking the whole node.

This module is an in-memory transform: it takes :class:`~di.models.KNode` objects and returns
:class:`~di.models.ARep` objects. It touches no database and no object storage.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from di.models import ARep, KNode, NodeType, RepType
from di.retrieval_client import (
    RetrievalClient,
    StubRetrievalClient,
    get_retrieval_client,
)

logger = logging.getLogger(__name__)

__all__ = ["generate_areps"]

# Node types that carry retrievable content worth re-phrasing. Structural nodes (document,
# section) are skipped — their children carry the content.
_CONTENT_NODE_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.chunk, NodeType.table, NodeType.figure, NodeType.fact}
)

# Representation types produced for every content node, regardless of media kind.
_BASE_REP_TYPES: tuple[RepType, ...] = (
    RepType.hypothetical_q,
    RepType.proposition,
    RepType.summary,
    RepType.alt_phrasing,
)

# Extra media-description reps, keyed by the node type that warrants them.
_MEDIA_REP_TYPES: dict[NodeType, tuple[RepType, ...]] = {
    NodeType.table: (RepType.table_desc,),
    NodeType.figure: (RepType.figure_desc,),
}

# Human-readable language names for prompting, by ISO code.
_LANG_NAMES: dict[str, str] = {"en": "English", "es": "Spanish"}

# Per-rep-type instruction. Each asks the model to emit *only* the representation text.
_REP_INSTRUCTIONS: dict[RepType, str] = {
    RepType.hypothetical_q: (
        "Write a single natural-language question that this passage would directly answer. "
        "Output only the question."
    ),
    RepType.proposition: (
        "Restate the key claim of this passage as one short, self-contained declarative "
        "proposition. Output only the proposition."
    ),
    RepType.summary: (
        "Summarize this passage in one concise sentence. Output only the summary."
    ),
    RepType.alt_phrasing: (
        "Paraphrase this passage using different words while preserving its meaning. "
        "Output only the paraphrase."
    ),
    RepType.table_desc: (
        "Describe what this table contains and what its rows and columns represent, in prose. "
        "Output only the description."
    ),
    RepType.figure_desc: (
        "Describe what this figure or image depicts, in prose. Output only the description."
    ),
}


def _node_source_lang(node: KNode) -> str:
    """Best-effort source language for ``node`` (provenance-free nodes default to English)."""
    return "en"


def _resolve_rep_types(node: KNode, rep_types: Sequence[RepType] | None) -> tuple[RepType, ...]:
    """Default rep-type set for ``node`` (base + media-specific), or the explicit override."""
    if rep_types is not None:
        return tuple(rep_types)
    return _BASE_REP_TYPES + _MEDIA_REP_TYPES.get(node.node_type, ())


def _node_text(node: KNode) -> str:
    """The text a representation is generated from: content, else title, else value text."""
    for candidate in (node.content, node.title, node.value_text):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def _instruction_for(rep_type: RepType) -> str:
    return _REP_INSTRUCTIONS.get(
        rep_type,
        "Rewrite this passage faithfully. Output only the rewritten text.",
    )


def _build_messages(rep_type: RepType, text: str, target_lang: str) -> list[dict[str, str]]:
    """Compose the chat messages for one representation in ``target_lang``."""
    lang_name = _LANG_NAMES.get(target_lang, target_lang)
    system = (
        "You generate retrieval-oriented representations of document passages for a search index. "
        f"Respond in {lang_name}. Do not add commentary, labels, or quotation marks."
    )
    user = f"{_instruction_for(rep_type)}\n\nPassage:\n{text}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _other_language(source_lang: str, languages: Sequence[str]) -> str | None:
    """The first supported language that is not ``source_lang`` (for the translation rep)."""
    for lang in languages:
        if lang != source_lang:
            return lang
    return None


async def generate_areps(
    nodes: list[KNode],
    *,
    client: RetrievalClient | StubRetrievalClient | None = None,
    languages: Sequence[str] = ("en", "es"),
    rep_types: Sequence[RepType] | None = None,
    max_concurrency: int = 4,
) -> list[ARep]:
    """Generate accessibility representations for the content-bearing nodes in ``nodes``.

    For each ``chunk``/``table``/``figure``/``fact`` node this produces the base reps
    (``hypothetical_q``, ``proposition``, ``summary``, ``alt_phrasing``), media-description reps for
    ``table``/``figure`` nodes (``table_desc``/``figure_desc``), and a ``translation`` rep into the
    other supported language (EN<->ES). Embeddings are **not** computed here.

    Args:
        nodes: Knowledge nodes to expand. Non-content nodes and empty-text nodes are skipped.
        client: Optional retrieval client. Defaults to :func:`get_retrieval_client` (the offline
            stub when ``DI_RETRIEVAL_STUB`` is set), so tests can run without the live service.
        languages: Supported languages; the first entry differing from a node's source language is
            the translation target.
        rep_types: Overrides the default rep-type set for *every* node when provided.
        max_concurrency: Upper bound on in-flight gateway calls.

    Returns:
        The generated :class:`~di.models.ARep` rows, in deterministic node-then-rep order. A
        representation whose gateway call fails or returns empty text is omitted.
    """
    if not nodes:
        return []

    gateway = client if client is not None else get_retrieval_client()
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def _generate_one(node: KNode, rep_type: RepType, target_lang: str) -> ARep | None:
        text = _node_text(node)
        if not text:
            return None
        messages = _build_messages(rep_type, text, target_lang)
        async with semaphore:
            try:
                rep_text, _usage = await gateway.llm_complete(messages, task="fast")
            except Exception:
                logger.exception(
                    "arep generation failed (node=%s rep_type=%s lang=%s)",
                    node.id,
                    rep_type,
                    target_lang,
                )
                return None
        rep_text = rep_text.strip()
        if not rep_text:
            return None
        return ARep(
            knode_id=node.id or "",
            client_id=node.client_id,
            doc_id=node.doc_id,
            version_id=node.version_id,
            path=node.path,
            rep_type=rep_type,
            rep_lang=target_lang,
            rep_text=rep_text,
            gen_model="retrieval:fast",
        )

    # Plan every (node, rep_type, lang) job up front so ordering is deterministic.
    plan: list[tuple[KNode, RepType, str]] = []
    for node in nodes:
        if node.node_type not in _CONTENT_NODE_TYPES:
            continue
        if not _node_text(node):
            continue
        source_lang = _node_source_lang(node)
        for rep_type in _resolve_rep_types(node, rep_types):
            plan.append((node, rep_type, source_lang))
        if rep_types is None:
            target = _other_language(source_lang, languages)
            if target is not None:
                plan.append((node, RepType.translation, target))

    results = await asyncio.gather(
        *(_generate_one(node, rep_type, lang) for node, rep_type, lang in plan)
    )
    return [rep for rep in results if rep is not None]
