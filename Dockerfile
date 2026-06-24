# Document Intelligence — application image.
# Lean build: core + [extract] deps (no heavy [ml] group; the gate runs on deterministic
# fallbacks). Model access (embeddings/LLM) is delegated to the retrieval gateway at runtime.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# System OCR deps: tesseract (images / scanned PDFs) + poppler (pdf2image rasterization).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# uv (fast installer) from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer (cached unless pyproject changes).
COPY pyproject.toml README.md ./
COPY di ./di
RUN uv pip install --system -e ".[extract]"

# Static console (served from /app/frontend/dist by the app).
COPY frontend ./frontend

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "di.app:app", "--host", "0.0.0.0", "--port", "8080"]
