"""Azure AI Vision Read OCR with a never-raises contract.

The public entrypoint :func:`extract_pages` turns raw document bytes into an
:class:`~di.models.OcrResult`. It is designed to *never* raise: any missing credential,
absent optional dependency, or runtime error degrades gracefully to a deterministic
fallback (a ``pypdf`` text-layer extraction when available, otherwise an empty result).

Engine resolution order:

1. ``azure-vision-read`` — Azure AI Vision Read OCR, used when
   :attr:`di.config.Settings.has_azure_vision` is true and the SDK imports.
   Azure Vision Read is image-first; for PDFs the pages are rasterized server-side or
   processed via the asynchronous Read operation (Computer Vision Read 3.2). Here we drive
   the synchronous ``ImageAnalysisClient`` (``azure-ai-vision-imageanalysis``) which returns
   a single logical page of line geometry; multipage PDFs would be handled by the async Read
   op upstream. We map whatever the SDK returns to ``OcrLine`` rows with bounding boxes.
2. ``pypdf`` — lazy text-layer extraction from a native PDF (no OCR, but free and offline).
3. ``none`` — empty result; nothing could be extracted.

All heavy/optional dependencies (azure SDK, pypdf) are imported lazily *inside* functions so
this module imports cleanly in any environment.
"""
from __future__ import annotations

import logging

from di.config import get_settings
from di.models import BBox, OcrLine, OcrResult

logger = logging.getLogger(__name__)

# Engine identifiers (kept as constants so callers can compare without typos).
ENGINE_AZURE = "azure-vision-read"
ENGINE_PYPDF = "pypdf"
ENGINE_NONE = "none"


def extract_pages(
    content: bytes,
    *,
    filename: str = "",
    mime: str | None = None,
) -> OcrResult:
    """Extract text + line geometry from ``content``; never raises.

    Args:
        content: Raw document bytes (image or PDF).
        filename: Optional original filename (informational; aids logging/heuristics).
        mime: Optional MIME type hint (e.g. ``"application/pdf"``).

    Returns:
        An :class:`~di.models.OcrResult`. ``engine`` is one of ``"azure-vision-read"``,
        ``"pypdf"``, or ``"none"``. On any failure or missing capability the function falls
        back to ``pypdf`` (if available and the bytes look like a PDF) and finally to an
        empty ``"none"`` result.
    """
    if not content:
        return OcrResult(engine=ENGINE_NONE, pages=0, text="", lines=[])

    settings = get_settings()
    if settings.has_azure_vision:
        try:
            result = _azure_read(content, filename=filename, mime=mime)
            if result is not None:
                return result
        except Exception:  # never propagate; fall back below
            logger.exception("Azure Vision Read OCR failed; falling back")

    return _fallback(content, filename=filename, mime=mime)


# ---------------------------------------------------------------------------
# Azure AI Vision Read
# ---------------------------------------------------------------------------
def _azure_read(
    content: bytes,
    *,
    filename: str,
    mime: str | None,
) -> OcrResult | None:
    """Run Azure AI Vision Read OCR. Lazy-imports the SDK; returns ``None`` if unavailable.

    Uses the modern ``azure-ai-vision-imageanalysis`` client with ``VisualFeatures.READ``.
    Image-first: a PDF would be rasterized/handled by the async Read op upstream; this
    synchronous path expects image bytes (or a single rendered page).
    """
    try:
        from azure.ai.vision.imageanalysis import ImageAnalysisClient
        from azure.ai.vision.imageanalysis.models import VisualFeatures
        from azure.core.credentials import AzureKeyCredential
    except ImportError:
        logger.info("azure-ai-vision-imageanalysis not installed; skipping Azure branch")
        return None

    settings = get_settings()
    client = ImageAnalysisClient(
        endpoint=settings.azure_vision_endpoint,
        credential=AzureKeyCredential(settings.azure_vision_key),
    )
    analysis = client.analyze(image_data=content, visual_features=[VisualFeatures.READ])
    return _map_azure_result(analysis)


def _map_azure_result(analysis: object) -> OcrResult:
    """Map an Azure ImageAnalysis result to :class:`OcrResult`.

    Defensive about the SDK's optional/nested attributes — the synchronous ImageAnalysis
    Read result exposes ``analysis.read.blocks[].lines[]``, each line carrying ``text`` and a
    ``bounding_polygon`` (list of points). We collapse the polygon to an axis-aligned bbox.
    """
    lines: list[OcrLine] = []
    text_parts: list[str] = []

    read = getattr(analysis, "read", None)
    blocks = getattr(read, "blocks", None) or []
    page_no = 1  # ImageAnalysis Read returns a single logical page.

    for block in blocks:
        for line in getattr(block, "lines", None) or []:
            line_text = getattr(line, "text", "") or ""
            text_parts.append(line_text)
            bbox = _polygon_to_bbox(getattr(line, "bounding_polygon", None), page_no)
            lines.append(
                OcrLine(
                    text=line_text,
                    page=page_no,
                    bbox=bbox,
                    confidence=_line_confidence(line),
                )
            )

    pages = 1 if lines else 0
    return OcrResult(
        engine=ENGINE_AZURE,
        pages=pages,
        text="\n".join(text_parts),
        lines=lines,
    )


def _polygon_to_bbox(polygon: object, page: int) -> BBox | None:
    """Collapse an Azure bounding polygon (list of points with .x/.y) to an axis-aligned BBox."""
    if not polygon:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for point in polygon:
        x = getattr(point, "x", None)
        y = getattr(point, "y", None)
        if x is None and isinstance(point, dict):
            x = point.get("x")
            y = point.get("y")
        if x is None or y is None:
            continue
        try:
            xs.append(float(x))
            ys.append(float(y))
        except (TypeError, ValueError):
            continue
    if not xs or not ys:
        return None
    return BBox(page=page, x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))


def _line_confidence(line: object) -> float | None:
    """Extract a per-line confidence if the SDK exposes one (it often does not)."""
    conf = getattr(line, "confidence", None)
    if conf is None:
        return None
    try:
        return float(conf)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------
def _fallback(
    content: bytes,
    *,
    filename: str,
    mime: str | None,
) -> OcrResult:
    """Best-effort offline extraction: pypdf text layer, else an empty ``none`` result."""
    if _looks_like_pdf(content, filename=filename, mime=mime):
        pdf_result = _pypdf_text_layer(content)
        if pdf_result is not None:
            return pdf_result
    return OcrResult(engine=ENGINE_NONE, pages=0, text="", lines=[])


def _looks_like_pdf(content: bytes, *, filename: str, mime: str | None) -> bool:
    """Heuristic: does this look like a PDF? (magic bytes / mime / extension)."""
    if content[:5] == b"%PDF-":
        return True
    if mime and "pdf" in mime.lower():
        return True
    return filename.lower().endswith(".pdf")


def _pypdf_text_layer(content: bytes) -> OcrResult | None:
    """Lazy ``pypdf`` text-layer extraction. Returns ``None`` if pypdf is absent or fails.

    This is *not* OCR — it only recovers an embedded text layer from native PDFs. We emit one
    :class:`OcrLine` per non-empty physical line, tagged with its page, but without geometry
    (pypdf does not expose reliable per-line bounding boxes here).
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return None

    import io

    try:
        reader = PdfReader(io.BytesIO(content))
        page_texts: list[str] = []
        lines: list[OcrLine] = []
        for page_index, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            page_texts.append(page_text)
            for raw_line in page_text.splitlines():
                stripped = raw_line.strip()
                if stripped:
                    lines.append(OcrLine(text=stripped, page=page_index, bbox=None, confidence=None))
    except Exception:  # corrupt/encrypted PDF, etc. — degrade to empty result
        logger.exception("pypdf text-layer extraction failed")
        return None

    text = "\n".join(t for t in page_texts if t).strip()
    if not text and not lines:
        return None
    return OcrResult(engine=ENGINE_PYPDF, pages=len(page_texts), text=text, lines=lines)
