"""Text/plain OCR passthrough — lets already-textual input drive the pipeline without Azure."""
from __future__ import annotations

from di.ocr import vision


def test_text_mime_passthrough():
    res = vision.extract_pages(b"Hello world\nLine two", filename="x.txt", mime="text/plain")
    assert res.engine == vision.ENGINE_TEXT
    assert res.pages == 1
    assert "Hello world" in res.text
    assert [ln.text for ln in res.lines] == ["Hello world", "Line two"]


def test_decodable_utf8_without_mime_passthrough():
    res = vision.extract_pages(b"PASSPORT REPUBLIC OF EXAMPLE", filename="doc")
    assert res.engine == vision.ENGINE_TEXT
    assert "PASSPORT" in res.text


def test_binary_bytes_fall_through_to_none():
    res = vision.extract_pages(bytes([0, 1, 2, 3, 255, 254, 200, 7]), filename="blob.bin")
    assert res.engine == vision.ENGINE_NONE
    assert res.text == ""


def test_empty_is_none():
    res = vision.extract_pages(b"")
    assert res.engine == vision.ENGINE_NONE
