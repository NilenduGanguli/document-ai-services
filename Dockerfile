# Document Intelligence — application image (multi-stage).
#
# Stage 1 compiles the React console with Vite; stage 2 is the lean Python runtime and copies in
# only the built `dist` (never node_modules). Building the UI inside the image keeps the container
# reproducible from source alone — no pre-built artifact has to be committed or shipped alongside.
#
# Runtime deps: core + [extract] + [s3]. The heavy [ml] group is deliberately excluded (the gate
# falls back to deterministic anchors); model access is delegated to the retrieval gateway.

# ---------------------------------------------------------------- stage 1: frontend
FROM node:20-slim AS frontend

WORKDIR /ui
# Dependency layer (cached unless the manifest changes).
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- stage 2: runtime
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
RUN uv pip install --system -e ".[extract,s3]"

# Compiled console only — served from /app/frontend/dist by the app.
COPY --from=frontend /ui/dist ./frontend/dist

# Default local-blob location; mount a volume here when BLOB_BACKEND=local.
RUN mkdir -p /data/blobs

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Run as a non-root user (the app never needs to write outside /data).
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /data
USER appuser

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "di.app:app", "--host", "0.0.0.0", "--port", "8080"]
