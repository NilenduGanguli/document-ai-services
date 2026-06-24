"""Anthropic-style contextual retrieval prefixes for subtree nodes.

Implements `Contextual Retrieval <https://www.anthropic.com/news/contextual-retrieval>`_: before a
content-bearing node (a chunk / table / figure / fact) is embedded, we ask the LLM for a short
50-100 token blurb that *situates* that node within the whole document. Prepending this blurb to the
node's content sharply improves retrieval recall because each chunk carries the surrounding context
("This section of the 2023 W-2 reports federal income tax withheld for ...") rather than standing
alone with dangling pronouns and undefined references.

This module is a pure in-memory transform: it takes a list of :class:`~di.models.KNode` objects and
returns the *same* list with ``context_prefix`` populated on content-bearing nodes. It never touches
the database. All model access goes through the retrieval-service gateway
(:func:`di.retrieval_client.get_retrieval_client`), so it runs fully offline against the
``StubRetrievalClient``.

Concurrency is bounded by an :class:`asyncio.Semaphore`; each gateway call is individually guarded so
one failure (or a node with no content) leaves that node's ``context_prefix`` as ``None`` without
aborting the batch.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from di.models import KNode, NodeType
from di.retrieval_client import get_retrieval_client

logger = logging.getLogger(__name__)

__all__ = ["add_context_prefixes"]

# Node types that carry retrieval-bearing content and therefore deserve a situating prefix.
# Document / section roots are structural-only and are skipped.
_CONTENT_NODE_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.chunk, NodeType.table, NodeType.figure, NodeType.fact}
)

# Cap the document text we feed into each prompt. ~12k chars keeps the prompt cheap and well within
# context limits while still giving the model enough of the document to situate a node.
_MAX_DOC_CHARS = 12_000

# A node's own content is also bounded so a single huge node cannot blow up the prompt.
_MAX_NODE_CHARS = 4_000

_SYSTEM_PROMPT = (
    "You write short contextual blurbs for a retrieval index. Given a whole document and one "
    "excerpt from it, write a 50-100 token blurb that situates the excerpt within the overall "
    "document so it can be understood and retrieved on its own. State what the excerpt is about and "
    "how it relates to the document. Respond with ONLY the blurb text -- no preamble, no quotes, no "
    "labels."
)


class _LLMClient(Protocol):
    """Structural type for the gateway calls this module makes (live or stub client)."""

    async def llm_complete(
        self,
        messages: list[dict[str, str]],
        *,
        task: str = ...,
        temperature: float = ...,
        max_tokens: int = ...,
        response_format: str = ...,
    ) -> tuple[str, dict[str, int]]: ...


def _node_content(node: KNode) -> str:
    """Best-effort textual content for a node, preferring ``content`` then a title/value combo.

    Fact nodes often carry their payload in ``value_text`` / ``attribute_key`` rather than
    ``content``, so we synthesise a small descriptor for them.
    """
    if node.content and node.content.strip():
        return node.content.strip()

    parts: list[str] = []
    if node.title and node.title.strip():
        parts.append(node.title.strip())
    if node.attribute_key:
        value = node.value_text or ""
        parts.append(f"{node.attribute_key}: {value}".strip())
    return "\n".join(p for p in parts if p).strip()


def _build_messages(doc_text: str, node_content: str) -> list[dict[str, str]]:
    """Assemble the chat messages for one node's contextual blurb request."""
    user = (
        "<document>\n"
        f"{doc_text}\n"
        "</document>\n\n"
        "<excerpt>\n"
        f"{node_content}\n"
        "</excerpt>\n\n"
        "Write the 50-100 token contextual blurb for the excerpt above."
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


async def _context_for_node(
    node: KNode,
    *,
    doc_text: str,
    client: _LLMClient,
    semaphore: asyncio.Semaphore,
) -> None:
    """Populate ``node.context_prefix`` in place. On any error, leave it as ``None``.

    Skips nodes with no usable content (the prefix stays ``None``). Guarded so a single failed
    gateway call never aborts the surrounding :func:`asyncio.gather`.
    """
    content = _node_content(node)
    if not content:
        return

    messages = _build_messages(doc_text, content[:_MAX_NODE_CHARS])
    async with semaphore:
        try:
            text, _usage = await client.llm_complete(
                messages,
                task="contextual",
                temperature=0,
                max_tokens=180,
            )
        except Exception as exc:  # noqa: BLE001 - one node failing must not sink the batch
            logger.warning(
                "contextual prefix failed for node %s: %s", node.id or node.path, exc
            )
            return

    prefix = text.strip()
    node.context_prefix = prefix or None


async def add_context_prefixes(
    nodes: list[KNode],
    *,
    full_doc_text: str,
    client: _LLMClient | None = None,
    max_concurrency: int = 4,
) -> list[KNode]:
    """Generate situating context prefixes for content-bearing nodes (Contextual Retrieval).

    For every chunk / table / figure / fact node the LLM is asked for a 50-100 token blurb that
    situates the node within ``full_doc_text``; the blurb is stored on ``node.context_prefix``.
    Document and section roots (which have no retrieval-bearing content of their own) are skipped and
    keep ``context_prefix=None``.

    The same ``nodes`` list is returned, mutated in place. The transform is in-memory only: it never
    touches the database. Concurrency is bounded by ``max_concurrency`` via an
    :class:`asyncio.Semaphore`, and each per-node gateway call is individually guarded so a failure
    leaves that node's prefix at ``None`` without aborting the batch.

    Args:
        nodes: Subtree nodes to annotate (mutated in place).
        full_doc_text: The full document text used to situate each node (capped to ~12k chars).
        client: Optional model gateway; defaults to :func:`di.retrieval_client.get_retrieval_client`.
        max_concurrency: Maximum simultaneous in-flight gateway calls (clamped to ``>= 1``).

    Returns:
        The same ``nodes`` list, with ``context_prefix`` set on content-bearing nodes.
    """
    client = client or get_retrieval_client()
    doc_text = (full_doc_text or "").strip()[:_MAX_DOC_CHARS]
    semaphore = asyncio.Semaphore(max(max_concurrency, 1))

    targets = [n for n in nodes if n.node_type in _CONTENT_NODE_TYPES]
    if targets:
        await asyncio.gather(
            *(
                _context_for_node(
                    node, doc_text=doc_text, client=client, semaphore=semaphore
                )
                for node in targets
            )
        )
    return nodes
