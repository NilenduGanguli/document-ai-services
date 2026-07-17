"""Authentication + authorization for MCP tool calls.

Mirrors the REST dependency chain (``di.auth.require_principal`` / ``require_scope`` /
``authorize_client``) but adapted to the MCP call model: the API key rides on the ``X-API-KEY``
header of the streamable-HTTP request, reachable inside a tool via the injected
:class:`~mcp.server.fastmcp.Context` (``ctx.request_context.request`` is the Starlette request that
carried the JSON-RPC message), and ``client_id`` is a tool argument rather than a path parameter.

There is exactly one key-resolution code path (``di.auth.resolve_principal``) shared with REST, so
a key that is unknown/expired/disabled fails identically here, and the per-key rate-limit backstop
still applies. A failure raises :class:`MCPAuthError`, which the SDK surfaces to the caller as a
tool error rather than leaking a stack trace.
"""
from __future__ import annotations

from typing import Any

from di import ratelimit
from di.auth import API_KEY_HEADER, Principal, resolve_principal
from di.config import get_settings

# Reuse the REST layer's local-dev sentinel so an auth-disabled demo behaves identically here.
_LOCAL_DEV_KEY_ID = "local-dev"


class MCPAuthError(Exception):
    """An MCP call was unauthenticated or unauthorized. Surfaces to the caller as a tool error."""


def _header(ctx: Any, name: str) -> str | None:
    """Best-effort read of a request header from the tool's Context.

    ``ctx.request_context.request`` is the Starlette request for the HTTP transport; it can be
    ``None`` for non-HTTP transports (not used here). Never raises — a missing request just means
    "no key present", handled as an auth failure by the caller.
    """
    try:
        request = ctx.request_context.request
    except Exception:  # noqa: BLE001 - no request context => treat as no header
        return None
    if request is None:
        return None
    try:
        return request.headers.get(name)
    except Exception:  # noqa: BLE001 - defensive; unexpected request shape
        return None


async def authenticate(ctx: Any) -> Principal:
    """Authenticate an MCP caller from the ``X-API-KEY`` header on the tool's Context.

    Returns a wildcard local-dev principal without a DB round-trip when ``auth_enabled`` is False,
    exactly like the REST path. Otherwise resolves the key (shared cache + failed-auth backstop)
    and enforces the per-key rate limit.

    Raises:
        MCPAuthError: when the header is missing, the key is unknown/expired/disabled, or the key's
            rate-limit backstop is exhausted.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return Principal(key_id=_LOCAL_DEV_KEY_ID, name=_LOCAL_DEV_KEY_ID,
                         client_ids=["*"], scopes=["*"])

    raw_key = _header(ctx, API_KEY_HEADER)
    if not raw_key:
        raise MCPAuthError(f"missing API key: supply the {API_KEY_HEADER} header")
    principal = await resolve_principal(raw_key)
    if principal is None:
        raise MCPAuthError("invalid or disabled API key")

    if settings.rate_limit_enabled and principal.key_id != _LOCAL_DEV_KEY_ID:
        rps = principal.rate_limit_rps or settings.rate_limit_default_rps
        allowed, _retry_after = ratelimit.check_rate_limit(principal.key_id, rps=rps)
        if not allowed:
            raise MCPAuthError("rate limit exceeded for this API key")
    return principal


def authorize(principal: Principal, *, scope: str, client_id: str) -> None:
    """Assert the principal holds ``scope`` and may act on ``client_id``.

    Raises:
        MCPAuthError: 403-equivalent — missing scope, or no tenant grant for ``client_id``.
    """
    if not principal.has_scope(scope):
        raise MCPAuthError(f"missing required scope: {scope}")
    if not principal.can_access(client_id):
        raise MCPAuthError(f"not authorized for client: {client_id}")


async def require(ctx: Any, *, scope: str, client_id: str) -> Principal:
    """Authenticate then authorize in one call — the standard guard at the top of every tool."""
    principal = await authenticate(ctx)
    authorize(principal, scope=scope, client_id=client_id)
    return principal


__all__ = ["MCPAuthError", "authenticate", "authorize", "require"]
