"""FastAPI application factory + lifespan.

Startup: open the DB pool, apply migrations under an advisory lock, seed the bootstrap API key,
and (best-effort) learn the embedding dimension from the retrieval service's ``/api/models``.
Every dependency's outcome is recorded in the readiness registry, so ``/readyz`` can tell a load
balancer the truth even when the process is alive but degraded — ``/health`` stays a pure liveness
probe.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from di import audit, auth, ingest_runner, observability, posture
from di.audit import AccessRecord, AuditUnavailable, resolve_audit_client_id
from di.config import get_settings
from di.db import (
    access_log_partition_horizon_months,
    assert_rls_posture,
    close_pool,
    init_pool,
    open_migration_connection,
    pgvector_available,
    run_migrations,
    set_embedding_dim,
    verify_migrations,
)
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


def _csp_header() -> str:
    """CSP for the SPA shell, derived from the actual built bundle (frontend/index.html +
    frontend/public/theme-init.js) rather than a generic template:

    - script-src 'self' — the bundle has no inline <script>; the pre-paint theme-setter lives in
      the same-origin theme-init.js precisely so this can stay strict.
    - style-src 'self' 'unsafe-inline' — React's `style={}` props render as inline `style`
      attributes; there is no practical per-element nonce/hash story for those, so this is the one
      deliberate loosening.
    - img-src 'self' data: — the favicon is an inlined `data:image/svg+xml` URI.
    - connect-src 'self' — all API calls are same-origin (frontend/src/lib/api.ts).

    Verified against the built console (must render, not blank) before enabling in
    tools/smoke_test.py — a bundle change that adds a new origin needs to update this function.
    """
    return (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; frame-ancestors 'none'"
    )


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
        return FileResponse(
            index, media_type="text/html",
            headers={"cache-control": "no-cache", "content-security-policy": _csp_header()},
        )

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
    except Exception as exc:  # noqa: BLE001 - see is_production branch below
        READINESS.set("db", False, f"connection failed: {exc}")
        logger.exception("database unreachable at startup")
        if settings.is_production:
            # A transient outage in local/dev degrades gracefully (READINESS already says so).
            # In production, staying alive here means the process serves traffic (or gets probed
            # as live) having never run the posture guard below — the exact fail-open this
            # closes. Propagate so uvicorn treats this as a failed boot.
            raise
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

    migrations_ok = False
    try:
        if settings.migrations_mode == "auto":
            conn = await open_migration_connection(settings)
            try:
                await run_migrations(settings, connection=conn)
            finally:
                await conn.close()
            READINESS.set("migrations", True, "applied (mode=auto)")
        elif settings.migrations_mode == "verify":
            await verify_migrations(settings)
            READINESS.set("migrations", True, "verified against the ledger (mode=verify)")
        else:  # "off"
            READINESS.set("migrations", True, "skipped (mode=off)")
        migrations_ok = True
    except Exception as exc:  # noqa: BLE001 - see is_production branch below
        READINESS.set("migrations", False, f"failed: {exc}")
        logger.exception("migrations failed")
        if settings.is_production:
            # Schema drift or a failed apply is a *positively observed* violation, not a
            # transient fault — refuse to serve rather than run against an unknown schema.
            raise

    if migrations_ok:
        try:
            violations = await assert_rls_posture(settings)
            if violations:
                detail = "; ".join(violations)
                READINESS.set("rls", False, detail)
                logger.critical("RLS posture violations: %s", detail)
                if settings.is_production:
                    raise RuntimeError(f"RLS posture violations: {detail}")
            else:
                READINESS.set("rls", True, "tenant_isolation verified on every tenant table; "
                              "runtime role is least-privilege")
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - see is_production branch below
            READINESS.set("rls", False, f"posture check failed: {exc}")
            logger.exception("could not evaluate RLS posture")
            if settings.is_production:
                raise
    else:
        READINESS.set("rls", False, "skipped: migrations did not complete")

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

    if settings.access_audit_enabled:
        audit.start_writer()
        try:
            horizon = await access_log_partition_horizon_months(settings)
            ok = horizon >= 1
            READINESS.set("audit", ok,
                          f"writer started; partition horizon {horizon} month(s)" if ok else
                          f"partition horizon is only {horizon} month(s) — re-run migrations",
                          horizon_months=horizon, strict=settings.access_audit_strict)
        except Exception as exc:  # noqa: BLE001
            READINESS.set("audit", False, f"partition horizon check failed: {exc}")
            logger.exception("could not check access-log partition horizon")
    else:
        READINESS.set("audit", True, "DISABLED — no read-side access audit is recorded",
                      enabled=False)

    # In strict mode a stalled writer must drain the replica (503 behind a green /readyz would
    # otherwise route traffic into guaranteed audit-unavailable 503s) — add it to the required
    # set only then, so non-strict/local deployments are never gated on audit health.
    if settings.access_audit_strict and "audit" not in observability.REQUIRED_COMPONENTS:
        observability.REQUIRED_COMPONENTS = (*observability.REQUIRED_COMPONENTS, "audit")


async def _shutdown() -> None:
    await ingest_runner.drain()
    await audit.stop_writer()
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
    # First statement, before FastAPI() exists: a misconfigured production instance must never
    # construct an app object, let alone serve a request. See di/posture.py.
    posture.assert_production_posture(settings)
    READINESS.set("posture", True, "static config checks passed"
                  if settings.is_production else "non-production: guards inactive")
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

    @app.middleware("http")
    async def _access_audit_middleware(request: Request, call_next):
        """Read-side access audit — "who read this client's data, and did they see it masked?"

        Only requests that resolve to a tenant ``client_id`` are recorded (health/metrics/console
        assets are not). Routing has already happened by the time ``call_next`` returns, so
        ``request.scope["route"]``/``path_params`` are read AFTER it, not before (Starlette
        populates them during dispatch, which happens inside ``call_next`` for this middleware
        style). HTTPException-based auth failures (401/403/429) come back as normal responses
        here — Starlette's ExceptionMiddleware, which sits closer to routing than this
        middleware, already converted them — so they are recorded with their real status; only a
        genuinely unhandled exception propagates as a raise, which is caught, recorded as a 500,
        and re-raised so the app's own exception handler still produces the response.
        """
        if not settings.access_audit_enabled:
            return await call_next(request)
        request_id = str(uuid.uuid4())
        try:
            response = await call_next(request)
        except Exception:
            await _record_access_attempt(request, request_id, 500)
            raise
        override = await _record_access_attempt(request, request_id, response.status_code)
        return override or response

    async def _record_access_attempt(request: Request, request_id: str, status: int,
                                      ) -> Response | None:
        route = request.scope.get("route")
        route_path = getattr(route, "path", None)
        if not route_path or not route_path.startswith("/api/"):
            return None
        path_params = request.scope.get("path_params") or {}
        client_id = resolve_audit_client_id(
            path_params, request.query_params, getattr(request.state, "audit_client_id", None)
        )
        if not client_id:
            return None
        principal = getattr(request.state, "principal", None)
        record = AccessRecord(
            method=request.method, route=route_path, status=status,
            key_id=principal.key_id if principal else None,
            principal=principal.name if principal else None,
            client_id=client_id,
            masked=getattr(request.state, "audit_masked", None),
            request_id=request_id,
        )
        try:
            await audit.record_access(record)
        except AuditUnavailable:
            # Fail closed: the real response was already computed but is discarded here — the
            # client never sees it. The synchronous stdout log line was already emitted inside
            # record_access, so this request is not entirely unaudited even in this path.
            return JSONResponse({"detail": "audit unavailable"}, status_code=503)
        return None

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
