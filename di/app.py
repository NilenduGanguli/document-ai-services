"""FastAPI application factory + lifespan.

Startup: open the DB pool, apply migrations idempotently, and (best-effort) learn the embedding
dimension from the retrieval service's /api/models so runtime vector columns use the right dim.
Failures degrade gracefully — the app still boots so health/diagnostics remain reachable.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from di.config import get_settings
from di.db import close_pool, init_pool, run_migrations, set_embedding_dim
from di.retrieval_client import get_retrieval_client

logger = logging.getLogger(__name__)

# Static console (no build step): frontend/dist/{index.html, assets/*}. Served like retrieval.
_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


class _CachedStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            # Console assets are not content-hashed → revalidate so edits are picked up.
            resp.headers["cache-control"] = "no-cache"
        return resp


def _mount_frontend(app: FastAPI) -> None:
    """Serve the SPA from frontend/dist: /assets (cached), / (index), SPA fallback; /api/* 404s."""
    dist, index, assets = _FRONTEND_DIST, _FRONTEND_DIST / "index.html", _FRONTEND_DIST / "assets"
    if not index.is_file():
        logger.info("frontend dist not found at %s; UI disabled", dist)
        return
    if assets.is_dir():
        app.mount("/assets", _CachedStatic(directory=str(assets)), name="assets")

    def _index() -> FileResponse:
        # never cache the shell, so versioned asset refs (?v=) are always re-read on reload
        return FileResponse(index, media_type="text/html", headers={"cache-control": "no-cache"})

    @app.get("/", include_in_schema=False)
    async def _root() -> FileResponse:
        return _index()

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def _spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"detail": "API route not found"}, status_code=404)
        candidate = (dist / full_path).resolve()
        try:
            candidate.relative_to(dist.resolve())
        except ValueError:
            return _index()
        return FileResponse(candidate) if candidate.is_file() else _index()

    logger.info("serving frontend console from %s", dist)


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

    _mount_frontend(app)
    return app


app = create_app()
