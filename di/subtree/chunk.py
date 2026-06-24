"""Structure-aware text chunking.

Pure, dependency-free helpers that split document text into bounded, retrieval-sized chunks.
The chunker is *paragraph-aware*: it packs whole paragraphs (split on blank lines) up to a soft
token budget, and only hard-splits a single paragraph that is on its own larger than the budget —
in which case it carries an overlap of trailing tokens into the next slice so context is not lost
at the seam.

Token counts are estimated, not tokenized: ``len(text) // 4`` (~4 chars/token) is a cheap, stable
heuristic that matches the project's ``chunk_max_tokens`` / ``chunk_overlap_tokens`` settings well
enough for sizing. No network, no model, no DB.
"""
from __future__ import annotations

import re

# Roughly 4 characters per token for English/Spanish prose. Used only for sizing decisions.
_CHARS_PER_TOKEN = 4

# Collapse runs of blank lines (with optional surrounding whitespace) into paragraph breaks.
_PARA_SPLIT = re.compile(r"\n[ \t]*\n+")

# Whitespace run, used to split a paragraph into words for token-bounded hard-splitting.
_WS = re.compile(r"\s+")

# Below this many estimated tokens a standalone chunk is considered a "sliver". We avoid emitting
# slivers by merging them into an adjacent chunk unless that is genuinely impossible.
_MIN_CHUNK_TOKENS = 20


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text`` (``len // 4``, floored at 0).

    A deliberately cheap, deterministic heuristic — never tokenizes. Whitespace-only or empty
    strings estimate to 0.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    return len(stripped) // _CHARS_PER_TOKEN


def _split_paragraphs(text: str) -> list[str]:
    """Split ``text`` on blank-line boundaries into non-empty, stripped paragraphs."""
    return [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]


def _hard_split_paragraph(
    paragraph: str, *, max_tokens: int, overlap_tokens: int
) -> list[str]:
    """Word-boundary hard-split an overlong paragraph into ``<= max_tokens`` slices.

    Consecutive slices share roughly ``overlap_tokens`` worth of trailing words so context spans
    the seam. Splits on whitespace (never mid-word); if a single "word" alone exceeds the budget
    (e.g. a long URL or base64 blob) it is emitted as its own slice rather than dropped.
    """
    words = [w for w in _WS.split(paragraph.strip()) if w]
    if not words:
        return []

    max_chars = max(max_tokens, 1) * _CHARS_PER_TOKEN
    overlap_chars = max(min(overlap_tokens, max_tokens - 1), 0) * _CHARS_PER_TOKEN

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0  # running char length including single-space joins

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append(" ".join(current))
            current = []
            current_len = 0

    for word in words:
        add_len = len(word) + (1 if current else 0)
        if current and current_len + add_len > max_chars:
            flush()
            # Seed the next slice with an overlap tail taken from the words just emitted.
            if overlap_chars > 0 and chunks:
                tail: list[str] = []
                tail_len = 0
                for prev in reversed(chunks[-1].split(" ")):
                    extra = len(prev) + (1 if tail else 0)
                    if tail and tail_len + extra > overlap_chars:
                        break
                    tail.insert(0, prev)
                    tail_len += extra
                current = tail
                current_len = tail_len
        current.append(word)
        current_len += len(word) + (1 if current[:-1] else 0)

    flush()
    return chunks


def _merge_slivers(chunks: list[str], *, max_tokens: int) -> list[str]:
    """Fold tiny trailing/leading fragments into a neighbour where it stays within budget.

    Prevents the failure mode of emitting <~20-token slivers. A sliver is merged into the previous
    chunk when the combined estimate fits ``max_tokens``; otherwise into the next; otherwise it is
    left as-is (genuinely unavoidable).
    """
    if len(chunks) <= 1:
        return chunks

    out: list[str] = []
    for chunk in chunks:
        if (
            out
            and estimate_tokens(chunk) < _MIN_CHUNK_TOKENS
            and estimate_tokens(out[-1]) + estimate_tokens(chunk) <= max_tokens
        ):
            out[-1] = f"{out[-1]}\n\n{chunk}"
        else:
            out.append(chunk)

    # A leading sliver could not look back; try folding it forward into the second chunk.
    if (
        len(out) >= 2
        and estimate_tokens(out[0]) < _MIN_CHUNK_TOKENS
        and estimate_tokens(out[0]) + estimate_tokens(out[1]) <= max_tokens
    ):
        out[1] = f"{out[0]}\n\n{out[1]}"
        out.pop(0)

    return out


def chunk_text(
    text: str, *, max_tokens: int = 512, overlap_tokens: int = 64
) -> list[str]:
    """Split ``text`` into bounded, paragraph-aware chunks.

    Paragraphs (blank-line delimited) are packed greedily up to roughly ``max_tokens`` estimated
    tokens. A paragraph that alone exceeds the budget is hard-split on word boundaries with
    ``overlap_tokens`` of shared context between consecutive slices. Tiny slivers are merged into a
    neighbour where possible.

    Args:
        text: Source text. Empty / whitespace-only input yields an empty list.
        max_tokens: Soft per-chunk token ceiling (estimate = ``len // 4``). Must be positive.
        overlap_tokens: Approximate token overlap for hard-split paragraphs. Clamped to
            ``[0, max_tokens - 1]``.

    Returns:
        A list of chunk strings, each estimated at ``<= max_tokens`` tokens (a single oversized
        indivisible token may exceed it). Order preserves the source.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if not text or not text.strip():
        return []

    overlap_tokens = max(min(overlap_tokens, max_tokens - 1), 0)
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return []

    chunks: list[str] = []
    buffer: list[str] = []
    buffer_tokens = 0

    def flush_buffer() -> None:
        nonlocal buffer, buffer_tokens
        if buffer:
            chunks.append("\n\n".join(buffer))
            buffer = []
            buffer_tokens = 0

    for para in paragraphs:
        para_tokens = estimate_tokens(para)

        if para_tokens > max_tokens:
            # Oversized paragraph: flush what we have, then hard-split it on its own.
            flush_buffer()
            chunks.extend(
                _hard_split_paragraph(
                    para, max_tokens=max_tokens, overlap_tokens=overlap_tokens
                )
            )
            continue

        if buffer and buffer_tokens + para_tokens > max_tokens:
            flush_buffer()
        buffer.append(para)
        buffer_tokens += para_tokens

    flush_buffer()
    return _merge_slivers(chunks, max_tokens=max_tokens)


def chunk_section(
    title: str,
    body: str,
    *,
    max_tokens: int = 512,
    overlap_tokens: int = 64,
) -> list[str]:
    """Chunk a titled section, prefixing the (stripped) ``title`` to the first chunk.

    The title is attached to the leading chunk for retrieval context; remaining chunks are plain.
    If ``body`` is empty the title alone becomes a single chunk (when present).
    """
    title = title.strip()
    chunks = chunk_text(body, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
    if not chunks:
        return [title] if title else []
    if title:
        chunks[0] = f"{title}\n\n{chunks[0]}"
    return chunks
