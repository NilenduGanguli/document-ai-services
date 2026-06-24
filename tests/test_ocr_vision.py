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


def test_polygon_to_bbox_collapses_points():
    class _P:
        def __init__(self, x: float, y: float) -> None:
            self.x = x
            self.y = y

    poly = [_P(10, 20), _P(110, 22), _P(112, 60), _P(8, 58)]
    bbox = vision._polygon_to_bbox(poly, page=1)
    assert bbox is not None
    assert bbox.page == 1
    assert bbox.x0 == 8 and bbox.y0 == 20
    assert bbox.x1 == 112 and bbox.y1 == 60

    # dict-shaped points are also supported
    bbox2 = vision._polygon_to_bbox([{"x": 1, "y": 2}, {"x": 3, "y": 4}], page=2)
    assert bbox2 is not None and bbox2.page == 2 and bbox2.x0 == 1 and bbox2.x1 == 3

    assert vision._polygon_to_bbox(None, page=1) is None
    assert vision._polygon_to_bbox([], page=1) is None


def test_map_azure_result_with_fake_analysis():
    """Exercise the Azure result mapper against a fake SDK shape (no SDK required)."""

    class _Pt:
        def __init__(self, x: float, y: float) -> None:
            self.x = x
            self.y = y

    class _Line:
        def __init__(self, text: str, poly: list[_Pt], conf: float | None) -> None:
            self.text = text
            self.bounding_polygon = poly
            self.confidence = conf

    class _Block:
        def __init__(self, lines: list[_Line]) -> None:
            self.lines = lines

    class _Read:
        def __init__(self, blocks: list[_Block]) -> None:
            self.blocks = blocks

    class _Analysis:
        def __init__(self, read: _Read) -> None:
            self.read = read

    analysis = _Analysis(
        _Read(
            [
                _Block(
                    [
                        _Line("HELLO", [_Pt(0, 0), _Pt(50, 0), _Pt(50, 10), _Pt(0, 10)], 0.99),
                        _Line("WORLD", [_Pt(0, 12), _Pt(60, 12), _Pt(60, 22), _Pt(0, 22)], None),
                    ]
                )
            ]
        )
    )

    result = vision._map_azure_result(analysis)
    assert result.engine == vision.ENGINE_AZURE
    assert result.pages == 1
    assert result.text == "HELLO\nWORLD"
    assert len(result.lines) == 2
    assert result.lines[0].text == "HELLO"
    assert result.lines[0].bbox is not None and result.lines[0].bbox.x1 == 50
    assert result.lines[0].confidence == 0.99
    assert result.lines[1].confidence is None


def test_map_azure_result_empty_blocks():
    class _Analysis:
        read = None

    result = vision._map_azure_result(_Analysis())
    assert result.engine == vision.ENGINE_AZURE
    assert result.pages == 0
    assert result.text == ""
    assert result.lines == []
