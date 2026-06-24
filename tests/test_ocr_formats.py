"""Multi-format OCR dispatch tests (di.ocr.vision).

_detect_kind is pure and always tested. The docx path runs when python-docx is installed; the
image/PDF-OCR paths need Tesseract/poppler (skipped cleanly when absent — they return None).
"""
from __future__ import annotations

import io

from di.ocr import vision


def test_detect_kind_by_magic_and_ext():
    assert vision._detect_kind(b"%PDF-1.7 ...", "x.pdf", "application/pdf") == "pdf"
    assert vision._detect_kind(b"PK\x03\x04", "x.docx",
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document") == "docx"
    assert vision._detect_kind(b"\x89PNG\r\n\x1a\n", "x.png", "image/png") == "image"
    assert vision._detect_kind(b"\xff\xd8\xff\xe0", "x.jpg", "image/jpeg") == "image"
    assert vision._detect_kind(b"Plain text content", "x.txt", "text/plain") == "text"
    assert vision._detect_kind(bytes([0, 1, 2, 255]), "x.bin", None) == "unknown"


def test_extract_pages_never_raises_on_each_kind():
    # Garbage bytes for each kind must degrade to a valid OcrResult, not raise.
    for content, name, mime in [
        (b"%PDF-not-real", "x.pdf", "application/pdf"),
        (b"PK\x03\x04 not a real docx", "x.docx", None),
        (b"\x89PNG not real", "x.png", "image/png"),
        (b"\xff\xd8\xff not real", "x.jpg", "image/jpeg"),
    ]:
        res = vision.extract_pages(content, filename=name, mime=mime)
        assert res.engine in {vision.ENGINE_PYPDF, vision.ENGINE_DOCX, vision.ENGINE_TESSERACT,
                              vision.ENGINE_TEXT, vision.ENGINE_NONE}


def test_docx_extract_roundtrip():
    docx = __import__("importlib").import_module("docx") if _has("docx") else None
    if docx is None:
        import pytest
        pytest.skip("python-docx not installed")
    doc = docx.Document()
    for line in ["SOCIAL SECURITY ADMINISTRATION", "JANE A DOE", "536-90-4399"]:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    res = vision.extract_pages(buf.getvalue(), filename="card.docx")
    assert res.engine == vision.ENGINE_DOCX
    assert "536-90-4399" in res.text and "JANE A DOE" in res.text


def _has(mod: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(mod) is not None
