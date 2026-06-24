# Document Intelligence — application image.
# Lean build: core + [extract] deps (no heavy [ml] group; the gate runs on deterministic
# fallbacks). Model access (embeddings/LLM) is delegated to the retrieval gateway at runtime.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# uv (fast installer) from the official distroless image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer (cached unless pyproject changes).
COPY pyproject.toml README.md ./
COPY di ./di
RUN uv pip install --system -e ".[extract]"

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "di.app:app", "--host", "0.0.0.0", "--port", "8080"]
