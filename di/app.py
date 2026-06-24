"""FastAPI application factory + lifespan.

Startup: open the DB pool, apply migrations idempotently, and (best-effort) learn the embedding
dimension from the retrieval service's /api/models so runtime vector columns use the right dim.
Failures degrade gracefully — the app still boots so health/diagnostics remain reachable.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from di.config import get_settings
from di.db import close_pool, init_pool, run_migrations, set_embedding_dim
from di.retrieval_client import get_retrieval_client

logger = logging.getLogger(__name__)


async def _startup() -> None:
    settings = get_settings()
    await init_pool(settings)
    try:
        client = get_retrieval_client(settings)
        info = await client.models()
        if dim := info.get("embedding_dim"):
            set_embedding_dim(int(dim))
            logger.info("embedding dim set to %s from retrieval /api/models", dim)
        await client.aclose()
    except Exception:  # noqa: BLE001 - non-fatal; fall back to configured default dim
        logger.warning("could not fetch /api/models; using default embedding dim", exc_info=True)
    try:
        await run_migrations(settings)
    except Exception:  # noqa: BLE001 - boot in degraded mode rather than crash
        logger.exception("migrations failed; continuing in degraded mode")


async def _shutdown() -> None:
    await close_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.di_log_level)
    app = FastAPI(title=settings.app_name, version="0.1.0")

    @app.on_event("startup")
    async def _on_startup() -> None:  # pragma: no cover - lifespan glue
        await _startup()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:  # pragma: no cover - lifespan glue
        await _shutdown()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": settings.app_name}

    # Routers are included lazily so a partially-implemented tree still boots.
    from di.routers import clients, ingest, nodes, search

    app.include_router(ingest.router)
    app.include_router(clients.router)
    app.include_router(search.router)
    app.include_router(nodes.router)
    return app


app = create_app()
