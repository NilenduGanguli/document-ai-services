"""Unit tests for di.extract.deterministic.anchored_kv.

Pure-logic (rapidfuzz is a light, always-installed dep). Exercises:
  * same-line right-neighbour binding,
  * nearest-line-below binding,
  * fuzzy tolerance of an OCR typo in the label,
  * the no-geometry text-only fallback.
"""
from __future__ import annotations

from di.extract.deterministic.anchored_kv import (
    anchor_extract,
    bind_value,
    find_label_line,
)
from di.models import BBox, OcrLine


def _line(text: str, x0: float, y0: float, x1: float, y1: float, page: int = 1) -> OcrLine:
    return OcrLine(text=text, page=page, bbox=BBox(page=page, x0=x0, y0=y0, x1=x1, y1=y1))


def test_binds_value_to_right_neighbour() -> None:
    lines = [
        _line("Date of Birth", 10, 100, 120, 120),
        _line("1990-01-15", 140, 100, 230, 120),   # same row, to the right
        _line("Sex", 10, 140, 60, 160),
        _line("M", 140, 140, 160, 160),
    ]
    pairs = anchor_extract(lines, ["Date of Birth"])
    assert len(pairs) == 1
    label, value = pairs[0]
    assert label == "Date of Birth"
    assert value.text == "1990-01-15"


def test_picks_nearest_right_neighbour_by_smallest_gap() -> None:
    lines = [
        _line("Date of Birth", 10, 100, 120, 120),
        _line("FAR", 400, 100, 430, 120),          # same row but far away
        _line("1990-01-15", 140, 100, 230, 120),   # same row, closest to the label
    ]
    _, value = anchor_extract(lines, ["Date of Birth"])[0]
    assert value.text == "1990-01-15"


def test_binds_value_below_when_no_right_neighbour() -> None:
    lines = [
        _line("Date of Birth", 10, 100, 120, 120),
        _line("1990-01-15", 12, 130, 110, 150),    # directly beneath, horizontal overlap
        _line("Unrelated", 12, 400, 110, 420),     # far below — should not win
    ]
    label, value = anchor_extract(lines, ["Date of Birth"])[0]
    assert label == "Date of Birth"
    assert value.text == "1990-01-15"


def test_fuzzy_match_tolerates_ocr_typo() -> None:
    # "Date 0f Birth" — OCR misread 'o' as '0'.
    lines = [
        _line("Date 0f Birth", 10, 100, 120, 120),
        _line("1985-07-09", 140, 100, 230, 120),
    ]
    match = find_label_line(lines, ["Date of Birth"], fuzz_threshold=85)
    assert match is not None
    matched_label, idx = match
    assert matched_label == "Date of Birth"
    assert idx == 0

    pairs = anchor_extract(lines, ["Date of Birth"], fuzz_threshold=85)
    assert pairs and pairs[0][1].text == "1985-07-09"


def test_multiple_labels_each_bind_their_own_value() -> None:
    lines = [
        _line("Date of Birth", 10, 100, 120, 120),
        _line("1990-01-15", 140, 100, 230, 120),
        _line("Fecha de nacimiento", 10, 140, 180, 160),
        _line("01/15/1990", 200, 140, 290, 160),
    ]
    pairs = anchor_extract(lines, ["Date of Birth", "Fecha de nacimiento"])
    by_label = dict(pairs)
    assert by_label["Date of Birth"].text == "1990-01-15"
    assert by_label["Fecha de nacimiento"].text == "01/15/1990"


def test_no_match_below_threshold_returns_empty() -> None:
    lines = [
        _line("Totally Different Header", 10, 100, 220, 120),
        _line("some value", 240, 100, 330, 120),
    ]
    assert anchor_extract(lines, ["Date of Birth"], fuzz_threshold=90) == []


def test_text_only_fallback_without_geometry() -> None:
    # No bbox on any line -> substring-after-label fallback.
    lines = [
        OcrLine(text="Date of Birth: 1990-01-15", page=1),
        OcrLine(text="Sex: M", page=1),
    ]
    pairs = anchor_extract(lines, ["Date of Birth"])
    assert len(pairs) == 1
    label, value = pairs[0]
    assert label == "Date of Birth"
    assert value.text == "1990-01-15"
    assert value.bbox is None


def test_text_only_fallback_fuzzy_when_no_substring() -> None:
    # Label not an exact substring (typo) and no bbox: degrades to whole-line fuzzy match.
    lines = [OcrLine(text="Date 0f Birth 1990-01-15", page=1)]
    pairs = anchor_extract(lines, ["Date of Birth"], fuzz_threshold=80)
    assert pairs
    assert pairs[0][0] == "Date of Birth"


def test_bind_value_returns_none_without_bbox() -> None:
    lines = [OcrLine(text="Date of Birth", page=1)]
    assert bind_value(lines, 0) is None


def test_empty_inputs() -> None:
    assert anchor_extract([], ["Date of Birth"]) == []
    assert anchor_extract([_line("Date of Birth", 0, 0, 10, 10)], []) == []
