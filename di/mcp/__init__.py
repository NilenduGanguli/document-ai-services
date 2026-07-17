"""MCP (Model Context Protocol) server — the agent-facing tool surface.

Exposes the platform's read/search/ingest capabilities as MCP tools and resources so other agents
can use the services. It mounts onto the SAME FastAPI app (``di.app``) as an ASGI sub-app, so it
ships in the same image and runs in the same process/container as the REST API (and, in embedded
mode, the ingest worker) — see :func:`di.mcp.server.build_mcp`.

Security is identical to the REST side and by construction cannot diverge: every tool authenticates
the same ``X-API-KEY`` header via :func:`di.auth.resolve_principal`, checks the same scopes, and
touches data only through ``di.store`` / ``di.jobs`` under ``acquire(client_id)`` — so per-tenant
RLS is preserved with no new isolation surface. Admin/destructive operations are deliberately NOT
exposed here.
"""
from __future__ import annotations

from di.mcp.server import build_mcp

__all__ = ["build_mcp"]
