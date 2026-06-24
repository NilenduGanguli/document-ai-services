"""Unit tests for structure-aware chunking (di.subtree.chunk).

Pure-logic: no DB, no network, no optional deps. Exercises paragraph packing, hard-split overlap,
sliver merging, and empty/short-input handling.
"""
from __future__ import annotations

import pytest

from di.subtree.chunk import (
    chunk_section,
    chunk_text,
    estimate_tokens,
)

# 4 chars/token heuristic used by the module.
_CHARS_PER_TOKEN = 4


def _tok(text: str) -> int:
    return estimate_tokens(text)


def test_empty_and_whitespace_input() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  \t ") == []
    assert chunk_text("\n\n\n") == []


def test_short_input_single_chunk() -> None:
    text = "Just a short sentence about a passport."
    chunks = chunk_text(text, max_tokens=512)
    assert chunks == [text]


def test_multi_paragraph_packs_into_bounded_pieces() -> None:
    # Build several paragraphs, each ~40 estimated tokens (160 chars).
    para = ("alpha bravo charlie delta " * 6).strip()
    assert 30 <= _tok(para) <= 60
    text = "\n\n".join(para for _ in range(10))

    max_tokens = 80
    chunks = chunk_text(text, max_tokens=max_tokens, overlap_tokens=8)

    assert len(chunks) > 1  # had to split across multiple chunks
    for c in chunks:
        # Packing should respect the soft ceiling (whole paragraphs only, none oversized here).
        assert _tok(c) <= max_tokens, (_tok(c), max_tokens)
    # No content lost: every paragraph instance is present across the chunks.
    assert "\n\n".join(chunks).count("alpha bravo charlie delta") == \
        text.count("alpha bravo charlie delta")


def test_overlong_paragraph_hard_split_has_overlap() -> None:
    # One paragraph (no blank lines) far larger than the budget => hard split.
    words = [f"w{i:04d}" for i in range(400)]  # 400 distinct words, ~5 chars each
    para = " ".join(words)

    max_tokens = 40
    overlap_tokens = 10
    chunks = chunk_text(para, max_tokens=max_tokens, overlap_tokens=overlap_tokens)

    assert len(chunks) > 2
    # Each slice within budget.
    for c in chunks:
        assert _tok(c) <= max_tokens, _tok(c)

    # Overlap present: trailing words of chunk N reappear as leading words of chunk N+1.
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        prev_words = prev.split()
        next_words = nxt.split()
        assert prev_words[-1] in next_words, (prev_words[-1], next_words[:5])
        # The shared prefix of nxt should be a tail of prev (contiguous overlap).
        assert next_words[0] in prev_words


def test_no_tiny_slivers_emitted() -> None:
    # A big paragraph followed by a one-word paragraph: the sliver must fold into a neighbour,
    # not stand alone as a <20-token chunk.
    big = ("lorem ipsum dolor sit amet " * 20).strip()  # well over a small budget
    text = f"{big}\n\ntiny"
    chunks = chunk_text(text, max_tokens=60, overlap_tokens=8)
    assert chunks  # produced something
    # 'tiny' must not be its own standalone sliver chunk.
    assert "tiny" not in [c.strip() for c in chunks]
    assert any("tiny" in c for c in chunks)


def test_overlap_clamped_when_exceeding_max_tokens() -> None:
    # overlap_tokens >= max_tokens must not loop/blow up; clamp to max_tokens-1.
    words = [f"x{i:03d}" for i in range(200)]
    para = " ".join(words)
    chunks = chunk_text(para, max_tokens=20, overlap_tokens=999)
    assert len(chunks) > 1
    for c in chunks:
        assert _tok(c) <= 20


def test_invalid_max_tokens_raises() -> None:
    with pytest.raises(ValueError):
        chunk_text("anything", max_tokens=0)


def test_chunk_section_prefixes_title_on_first_chunk() -> None:
    para = ("foo bar baz qux " * 8).strip()
    body = "\n\n".join(para for _ in range(5))
    title = "Section 1: Identity"

    chunks = chunk_section(title, body, max_tokens=80, overlap_tokens=8)
    assert chunks
    assert chunks[0].startswith(title)
    # Title appears only once (on the first chunk).
    assert sum(c.count(title) for c in chunks) == 1


def test_chunk_section_empty_body_returns_title() -> None:
    assert chunk_section("Title Only", "") == ["Title Only"]
    assert chunk_section("", "") == []


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("   ") == 0
    assert estimate_tokens("a" * 4) == 1
    assert estimate_tokens("a" * 40) == 10
