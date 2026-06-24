# Mock Azure OCR (Computer Vision Read v3.2) — plain Python + Tesseract, no Azure SDK.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY app.py ./
RUN uv pip install --system fastapi "uvicorn[standard]" pytesseract pillow

EXPOSE 5000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000"]
