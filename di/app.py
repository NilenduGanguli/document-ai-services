"""FastAPI application factory + lifespan.

Startup: open the DB pool, apply migrations under an advisory lock, seed the bootstrap API key,
and (best-effort) learn the embedding dimension from the retrieval service's ``/api/models``.
Every dependency's outcome is recorded in the readiness registry, so ``/readyz`` can tell a load
balancer the truth even when the process is alive but degraded — ``/health`` stays a pure liveness
probe.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from di import auth, ingest_runner, observability
from di.config import get_settings
from di.db import close_pool, init_pool, pgvector_available, run_migrations, set_embedding_dim
from di.observability import READINESS
from di.retrieval_client import get_retrieval_client
from di.storage import get_blob_store

logger = logging.getLogger(__name__)

# Compiled React console: frontend/dist/{index.html, assets/*} — built by `npm run build`.
_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


class _HashedStatic(StaticFiles):
    """Vite emits content-hashed asset filenames, so they are safe to cache immutably."""

    async def get_response(self, path: str, scope: Any) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["cache-control"] = "public, max-age=31536000, immutable"
        return resp


def _mount_frontend(app: FastAPI) -> None:
    """Serve the SPA from frontend/dist: /assets (immutable), / (index), SPA fallback; /api/* 404s."""
    dist, index, assets = _FRONTEND_DIST, _FRONTEND_DIST / "index.html", _FRONTEND_DIST / "assets"
    if not index.is_file():
        logger.info("frontend dist not found at %s; UI disabled", dist)
        return
    if assets.is_dir():
        app.mount("/assets", _HashedStatic(directory=str(assets)), name="assets")

    def _index() -> FileResponse:
        # never cache the shell, so a new build's hashed asset refs are picked up on reload
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

    try:
        await init_pool(settings)
        READINESS.set("db", True, "connected", host=settings.pg_host, database=settings.pg_database)
    except Exception as exc:  # noqa: BLE001 - stay up so /readyz can report the cause
        READINESS.set("db", False, f"connection failed: {exc}")
        logger.exception("database unreachable at startup")
        return

    try:
        client = get_retrieval_client(settings)
        info = await client.models()
        if dim := info.get("embedding_dim"):
            set_embedding_dim(int(dim))
            logger.info("embedding dim set to %s from retrieval /api/models", dim)
        await client.aclose()
        READINESS.set("retrieval", True, "live gateway" if not settings.di_retrieval_stub
                      else "in-process stub", stub=settings.di_retrieval_stub)
    except Exception as exc:  # noqa: BLE001 - non-fatal; fall back to configured default dim
        READINESS.set(
            "retrieval", settings.di_retrieval_stub,
            f"/api/models unreachable ({exc}); using default embedding dim "
            f"{settings.embedding_dim_default}",
            stub=settings.di_retrieval_stub,
        )
        logger.warning("could not fetch /api/models; using default embedding dim")

    try:
        await run_migrations(settings)
        READINESS.set("migrations", True, "applied")
    except Exception as exc:  # noqa: BLE001 - boot degraded rather than crash, but SAY so
        READINESS.set("migrations", False, f"failed: {exc}")
        logger.exception("migrations failed; continuing in degraded mode")

    try:
        has_vec = await pgvector_available()
        READINESS.set("pgvector", True, "available" if has_vec
                      else "absent — search degrades to full-text only", enabled=has_vec)
    except Exception as exc:  # noqa: BLE001
        READINESS.set("pgvector", False, str(exc))

    try:
        health = await get_blob_store().health()
        READINESS.set("blob", bool(health.get("ok")), str(health.get("detail", "")),
                      backend=health.get("backend"))
    except Exception as exc:  # noqa: BLE001
        READINESS.set("blob", False, f"blob store unhealthy: {exc}")

    READINESS.set("ocr", True,
                  "azure read v3.2" if settings.has_azure_vision else "local tesseract fallback",
                  endpoint=settings.azure_vision_endpoint or "(none)",
                  configured=settings.has_azure_vision)

    if settings.auth_enabled:
        try:
            key_id = await auth.ensure_bootstrap_key()
            READINESS.set("auth", True, "enabled", bootstrap_seeded=bool(key_id))
        except Exception as exc:  # noqa: BLE001
            READINESS.set("auth", False, f"bootstrap key seeding failed: {exc}")
            logger.exception("could not seed the bootstrap API key")
    else:
        READINESS.set("auth", True, "DISABLED — every /api/v1 route is open", enabled=False)
        logger.warning("AUTH IS DISABLED (auth_enabled=false): all API routes are unauthenticated")


async def _shutdown() -> None:
    await ingest_runner.drain()
    await close_pool()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.di_log_level)
    app = FastAPI(
        title=settings.app_name,
        version="0.2.0",
        summary="Per-client KYC knowledge trees from documents — PII-safe, provenance-tracked.",
        lifespan=lifespan,
    )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        """Uniform error envelope so integrators have something reliable to key retries on."""
        logger.exception("unhandled error", exc_info=exc)
        observability.observe_stage("request", 0.0, ok=False)
        return JSONResponse({"detail": "internal server error"}, status_code=500)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        """Liveness only: the process is up. Use /readyz to decide whether to send traffic."""
        return {"status": "ok", "service": settings.app_name, "version": app.version}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> JSONResponse:
        """Readiness: per-dependency truth. 503 when a required component is down."""
        snap = READINESS.snapshot()
        ready = READINESS.ready()
        body = {
            "ready": ready,
            "degraded": READINESS.degraded(),
            "env": settings.di_env,
            "components": {k: v.model_dump() for k, v in snap.items()},
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    async def metrics() -> Response:
        """Prometheus exposition: gate decisions, LLM egress, stage timings, ingest outcomes."""
        payload, content_type = observability.metrics_response()
        return Response(content=payload, media_type=content_type)

    # Routers are included lazily so a partially-implemented tree still boots.
    from di.routers import admin, clients, ingest, jobs, nodes, search

    app.include_router(ingest.router)
    app.include_router(jobs.router)
    app.include_router(clients.router)
    app.include_router(search.router)
    app.include_router(nodes.router)
    app.include_router(admin.router)

    _mount_frontend(app)
    return app


app = create_app()
