"""Unit tests for di.ocr.vision — the never-raises OCR entrypoint.

Covers the deterministic fallback paths (no Azure creds) and verifies that the Azure branch
is properly guarded by ``Settings.has_azure_vision``. The Azure SDK is optional and lazily
imported, so the Azure-specific mapping is tested against a fake analysis object rather than
the real client.
"""
from __future__ import annotations

from di.models import OcrResult
from di.ocr import vision


def test_junk_bytes_no_creds_returns_result_no_raise(monkeypatch):
    """Junk bytes + no Azure creds -> a valid OcrResult, never an exception."""
    settings = vision.get_settings()
    monkeypatch.setattr(type(settings), "has_azure_vision", property(lambda self: False))

    result = vision.extract_pages(b"\x00\x01not a real document\xff", filename="junk.bin")
    assert isinstance(result, OcrResult)
    # No creds, not a PDF -> empty 'none' result.
    assert result.engine == vision.ENGINE_NONE
    assert result.pages == 0
    assert result.text == ""
    assert result.lines == []


def test_empty_content_returns_none_engine(monkeypatch):
    monkeypatch.setattr(
        type(vision.get_settings()), "has_azure_vision", property(lambda self: False)
    )
    result = vision.extract_pages(b"")
    assert result.engine == vision.ENGINE_NONE
    assert result.pages == 0


def test_azure_branch_guarded_by_settings(monkeypatch):
    """When has_azure_vision is False, _azure_read must never be called."""
    monkeypatch.setattr(
        type(vision.get_settings()), "has_azure_vision", property(lambda self: False)
    )

    called = {"azure": False}

    def _boom(*args, **kwargs):
        called["azure"] = True
        raise AssertionError("Azure path must not run without creds")

    monkeypatch.setattr(vision, "_azure_read", _boom)
    result = vision.extract_pages(b"%PDF-1.4 fake", filename="x.pdf")
    assert called["azure"] is False
    assert isinstance(result, OcrResult)
    # Bytes look like a PDF but are ASCII; without pypdf they decode via the text passthrough.
    assert result.engine in {vision.ENGINE_PYPDF, vision.ENGINE_NONE, vision.ENGINE_TEXT}


def test_azure_exception_falls_back(monkeypatch):
    """If creds are present but the Azure call blows up, we degrade — never raise."""
    monkeypatch.setattr(
        type(vision.get_settings()), "has_azure_vision", property(lambda self: True)
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated SDK failure")

    monkeypatch.setattr(vision, "_azure_read", _boom)
    result = vision.extract_pages(b"\x89PNG not really", filename="img.png", mime="image/png")
    assert isinstance(result, OcrResult)
    assert result.engine == vision.ENGINE_NONE


def test_looks_like_pdf_heuristics():
    assert vision._looks_like_pdf(b"%PDF-1.7 ...", filename="", mime=None) is True
    assert vision._looks_like_pdf(b"junk", filename="report.pdf", mime=None) is True
    assert vision._looks_like_pdf(b"junk", filename="x", mime="application/pdf") is True
    assert vision._looks_like_pdf(b"junk", filename="x.txt", mime="text/plain") is False


def test_bbox_from_polygon_v32():
    # v3.2 boundingBox is a flat [x1,y1,x2,y2,x3,y3,x4,y4]
    bbox = vision._bbox_from_polygon([10, 20, 110, 22, 112, 60, 8, 58], page=1)
    assert bbox is not None and bbox.page == 1
    assert bbox.x0 == 8 and bbox.y0 == 20 and bbox.x1 == 112 and bbox.y1 == 60
    assert vision._bbox_from_polygon(None, page=1) is None
    assert vision._bbox_from_polygon([1, 2, 3, 4], page=1) is None  # too short


def test_map_v32_payload():
    """Map an Azure Read v3.2 analyzeResults payload (the shape the cloud + mock both return)."""
    payload = {
        "status": "succeeded",
        "analyzeResult": {
            "readResults": [
                {"page": 1, "lines": [
                    {"text": "HELLO WORLD", "boundingBox": [0, 0, 60, 0, 60, 10, 0, 10],
                     "words": [{"text": "HELLO", "confidence": 0.99},
                               {"text": "WORLD", "confidence": 0.97}]},
                    {"text": "536-90-4399", "boundingBox": [0, 12, 80, 12, 80, 22, 0, 22],
                     "words": [{"text": "536-90-4399", "confidence": 0.95}]},
                ]},
            ]
        },
    }
    result = vision._map_v32(payload)
    assert result.engine == vision.ENGINE_AZURE and result.pages == 1
    assert result.text == "HELLO WORLD\n536-90-4399"
    assert len(result.lines) == 2
    assert result.lines[0].bbox is not None and result.lines[0].bbox.x1 == 60
    assert abs((result.lines[0].confidence or 0) - 0.98) < 1e-6


def test_map_v32_empty():
    assert vision._map_v32({"analyzeResult": {"readResults": []}}) is None
