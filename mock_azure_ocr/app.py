"""Mock Azure Computer Vision **Read v3.2** OCR service (for local testing without real Azure).

Implements the same REST contract document_intelligence's httpx client (and the real Azure
service) speak:

    POST /vision/v3.2/read/analyze            -> 202 + Operation-Location header
    GET  /vision/v3.2/read/analyzeResults/{id} -> {status, analyzeResult:{readResults:[...]}}

OCR is performed locally with Tesseract so responses reflect the uploaded image, returned in the
Azure v3.2 JSON shape. Built only from Python packages (FastAPI + pytesseract + Pillow) — no Azure
SDK. Swap this out for real Azure by pointing AZURE_VISION_ENDPOINT/KEY at your resource.
"""
from __future__ import annotations

import io
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="mock-azure-ocr-read-v3.2")
_JOBS: dict[str, dict] = {}


def _ocr_to_v32(content: bytes) -> dict:
    """Tesseract OCR an image and shape it as an Azure v3.2 readResult (page 1)."""
    import pytesseract
    from PIL import Image

    img = Image.open(io.BytesIO(content))
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    groups: dict[tuple, dict] = {}
    for i in range(len(data["text"])):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        try:
            conf = max(float(data["conf"][i]) / 100.0, 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        g = groups.setdefault(key, {"words": [], "x0": x, "y0": y, "x1": x + w, "y1": y + h})
        g["words"].append({"text": word, "confidence": round(conf, 3),
                           "boundingBox": [x, y, x + w, y, x + w, y + h, x, y + h]})
        g["x0"], g["y0"] = min(g["x0"], x), min(g["y0"], y)
        g["x1"], g["y1"] = max(g["x1"], x + w), max(g["y1"], y + h)
    lines = []
    for _, g in sorted(groups.items()):
        lines.append({
            "text": " ".join(w["text"] for w in g["words"]),
            "boundingBox": [g["x0"], g["y0"], g["x1"], g["y0"], g["x1"], g["y1"], g["x0"], g["y1"]],
            "words": g["words"],
        })
    return {"page": 1, "angle": 0.0, "width": img.width, "height": img.height,
            "unit": "pixel", "lines": lines}


@app.post("/vision/v3.2/read/analyze")
async def analyze(request: Request) -> Response:
    content = await request.body()
    op_id = uuid.uuid4().hex
    try:
        read_result = _ocr_to_v32(content)
        _JOBS[op_id] = {"status": "succeeded",
                        "analyzeResult": {"version": "3.2.0", "readResults": [read_result]}}
    except Exception as exc:  # noqa: BLE001 - report as a failed Read job
        _JOBS[op_id] = {"status": "failed", "errors": [{"message": str(exc)}]}
    op_url = str(request.base_url).rstrip("/") + f"/vision/v3.2/read/analyzeResults/{op_id}"
    return Response(status_code=202, headers={"Operation-Location": op_url})


@app.get("/vision/v3.2/read/analyzeResults/{op_id}")
async def analyze_results(op_id: str) -> JSONResponse:
    job = _JOBS.get(op_id)
    if job is None:
        return JSONResponse({"error": {"code": "404", "message": "operation not found"}},
                            status_code=404)
    return JSONResponse(job)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "mock-azure-ocr-read-v3.2"}
