"""Generic label-anchored key/value extraction from OCR lines.

This is a *pure* helper shared by jurisdiction-specific deterministic extractors. Given a set of
OCR lines (ideally with bbox geometry) and a list of candidate labels (e.g. ``["Date of Birth",
"Fecha de nacimiento"]``), it fuzzily locates the line carrying the label and binds the most
plausible *value* line using simple page geometry:

1. **Same-line right neighbour** — the line to the right of the label on the same row (smallest
   positive horizontal gap, with vertical overlap). This is the dominant form-field layout.
2. **Nearest line below** — failing that, the closest line beneath the label that horizontally
   overlaps it (stacked label-over-value layout).

When the lines carry no bbox geometry at all, it degrades to a **text-only fallback**: it finds
the label as a substring within a line and returns the text that follows it on the same line.

Only ``rapidfuzz`` (and, transitively, ``dateparser`` elsewhere) is required — both are light,
always-installed dependencies — so this module imports with no optional deps.
"""
from __future__ import annotations

from di.models import BBox, OcrLine

__all__ = ["anchor_extract", "find_label_line", "bind_value"]


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _vertical_overlap(a: BBox, b: BBox) -> float:
    """Height of the vertical intersection of two bboxes (0.0 when disjoint or different pages)."""
    if a.page != b.page:
        return 0.0
    top = max(a.y0, b.y0)
    bottom = min(a.y1, b.y1)
    return max(0.0, bottom - top)


def _horizontal_overlap(a: BBox, b: BBox) -> float:
    """Width of the horizontal intersection of two bboxes (0.0 when disjoint or different pages)."""
    if a.page != b.page:
        return 0.0
    left = max(a.x0, b.x0)
    right = min(a.x1, b.x1)
    return max(0.0, right - left)


def _label_offset(text: str, label: str) -> int:
    """Index just past a case-insensitive occurrence of ``label`` in ``text``; -1 if absent."""
    idx = text.lower().find(label.lower())
    if idx < 0:
        return -1
    return idx + len(label)


# ---------------------------------------------------------------------------
# Fuzzy label location
# ---------------------------------------------------------------------------
def find_label_line(
    lines: list[OcrLine],
    labels: list[str],
    *,
    fuzz_threshold: int = 85,
) -> tuple[str, int] | None:
    """Locate the OCR line that best carries any of ``labels``.

    Uses ``rapidfuzz`` ``token_set_ratio`` to tolerate OCR noise and word-order shuffling. Returns
    ``(matched_label, line_index)`` for the highest-scoring (label, line) pair at or above
    ``fuzz_threshold``; ``None`` if nothing clears the bar. ``rapidfuzz`` is a light, always-present
    dependency, so it is imported at call time only to keep the module body cheap.
    """
    from rapidfuzz import fuzz

    best_score = -1.0
    best: tuple[str, int] | None = None
    for idx, line in enumerate(lines):
        text = line.text
        if not text.strip():
            continue
        for label in labels:
            score = fuzz.token_set_ratio(label, text)
            if score >= fuzz_threshold and score > best_score:
                best_score = score
                best = (label, idx)
    return best


# ---------------------------------------------------------------------------
# Value binding by geometry
# ---------------------------------------------------------------------------
def bind_value(lines: list[OcrLine], label_idx: int) -> OcrLine | None:
    """Bind the value line for the label at ``lines[label_idx]`` using bbox geometry.

    Priority: (1) same-line right neighbour with vertical overlap and the smallest positive x-gap;
    (2) the nearest line below that horizontally overlaps the label. Returns ``None`` when the label
    line lacks a bbox or no geometric candidate is found.
    """
    label_line = lines[label_idx]
    lbox = label_line.bbox
    if lbox is None:
        return None

    right_best: OcrLine | None = None
    right_gap = float("inf")
    below_best: OcrLine | None = None
    below_gap = float("inf")

    for idx, cand in enumerate(lines):
        if idx == label_idx:
            continue
        cbox = cand.bbox
        if cbox is None or cbox.page != lbox.page:
            continue
        if not cand.text.strip():
            continue

        # (1) Same-line right neighbour: starts to the right, shares a vertical band.
        if cbox.x0 >= lbox.x1 - 1e-9 and _vertical_overlap(lbox, cbox) > 0.0:
            gap = cbox.x0 - lbox.x1
            if gap < right_gap:
                right_gap = gap
                right_best = cand

        # (2) Nearest line below: starts beneath the label, shares a horizontal band.
        if cbox.y0 >= lbox.y1 - 1e-9 and _horizontal_overlap(lbox, cbox) > 0.0:
            gap = cbox.y0 - lbox.y1
            if gap < below_gap:
                below_gap = gap
                below_best = cand

    if right_best is not None:
        return right_best
    return below_best


# ---------------------------------------------------------------------------
# Text-only fallback (no geometry)
# ---------------------------------------------------------------------------
def _text_only_extract(
    lines: list[OcrLine],
    labels: list[str],
    *,
    fuzz_threshold: int,
) -> list[tuple[str, OcrLine]]:
    """Fallback when lines carry no bbox: take the substring after the label on the same line.

    For each label, find the first line that *contains* the label (exact, case-insensitive) and
    yields non-empty trailing text. If no exact substring hit exists for any label, fall back to a
    single fuzzy match on the whole line and return that line verbatim as the value.
    """
    out: list[tuple[str, OcrLine]] = []
    for label in labels:
        for line in lines:
            off = _label_offset(line.text, label)
            if off < 0:
                continue
            tail = line.text[off:].lstrip(" :\t-=–—")
            if tail.strip():
                value_line = OcrLine(
                    text=tail.strip(),
                    page=line.page,
                    bbox=line.bbox,
                    confidence=line.confidence,
                )
                out.append((label, value_line))
                break
    if out:
        return out

    # No clean "label: value" substring anywhere — fall back to a single fuzzy line match.
    match = find_label_line(lines, labels, fuzz_threshold=fuzz_threshold)
    if match is not None:
        matched_label, idx = match
        return [(matched_label, lines[idx])]
    return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def anchor_extract(
    lines: list[OcrLine],
    labels: list[str],
    *,
    fuzz_threshold: int = 85,
) -> list[tuple[str, OcrLine]]:
    """Extract ``(matched_label, value_line)`` pairs by fuzzily anchoring on ``labels``.

    Algorithm:
      * Fuzzy-match (``rapidfuzz.token_set_ratio``, ``>= fuzz_threshold``) the line carrying a label.
      * Bind the value by geometry: same-line right neighbour first, else nearest line below.
      * If no line carries usable bbox geometry, degrade to a text-only fallback that returns the
        substring after the label on the same line.

    Returns at most one pair per distinct matched label (the first/best binding wins). An empty list
    means no label cleared ``fuzz_threshold`` or no value could be bound.
    """
    if not lines or not labels:
        return []

    has_geometry = any(line.bbox is not None for line in lines)
    if not has_geometry:
        return _text_only_extract(lines, labels, fuzz_threshold=fuzz_threshold)

    out: list[tuple[str, OcrLine]] = []
    seen_labels: set[str] = set()
    # Search per label so distinct labels can each bind their own value, not just the global best.
    for label in labels:
        match = find_label_line(lines, [label], fuzz_threshold=fuzz_threshold)
        if match is None:
            continue
        matched_label, idx = match
        if matched_label in seen_labels:
            continue
        value = bind_value(lines, idx)
        if value is None:
            # Geometry present overall but this label line had no bbox / no neighbour: try same-line tail.
            off = _label_offset(lines[idx].text, matched_label)
            if off >= 0:
                tail = lines[idx].text[off:].lstrip(" :\t-=–—")
                if tail.strip():
                    value = OcrLine(
                        text=tail.strip(),
                        page=lines[idx].page,
                        bbox=lines[idx].bbox,
                        confidence=lines[idx].confidence,
                    )
        if value is not None:
            out.append((matched_label, value))
            seen_labels.add(matched_label)
    return out
