"""Unit tests for the MCP endpoint's auth guard and tool registry.

Pure-logic — no DB, no network, no live MCP transport. The security-critical surface here is
di.mcp.auth (does an MCP call authenticate and authorize exactly like REST?) and the tool catalog
(are the read/ingest tools present, and are admin/destructive tools absent by construction?). The
full transport round-trip (real MCP client → /mcp → RLS-filtered read) is exercised live against a
running stack, mirroring the queue's verification split.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from di.auth import Principal
from di.config import get_settings
from di.mcp import auth as mcp_auth


def _ctx(api_key: str | None) -> SimpleNamespace:
    """A stand-in for the FastMCP Context: ctx.request_context.request.headers.get(name)."""
    headers = {"X-API-KEY": api_key} if api_key is not None else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=headers)))


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- authenticate
async def test_authenticate_missing_key_raises(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    get_settings.cache_clear()
    with pytest.raises(mcp_auth.MCPAuthError, match="missing API key"):
        await mcp_auth.authenticate(_ctx(None))


async def test_authenticate_bad_key_raises(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()

    async def _none(_raw):
        return None

    monkeypatch.setattr(mcp_auth, "resolve_principal", _none)
    with pytest.raises(mcp_auth.MCPAuthError, match="invalid or disabled"):
        await mcp_auth.authenticate(_ctx("di_wrong"))


async def test_authenticate_good_key_returns_principal(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()
    principal = Principal(key_id="k1", name="acme", client_ids=["acme"], scopes=["read"])

    async def _ok(_raw):
        return principal

    monkeypatch.setattr(mcp_auth, "resolve_principal", _ok)
    got = await mcp_auth.authenticate(_ctx("di_right"))
    assert got is principal


async def test_authenticate_auth_disabled_returns_wildcard(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    principal = await mcp_auth.authenticate(_ctx(None))
    assert principal.client_ids == ["*"] and principal.scopes == ["*"]


# --------------------------------------------------------------------------- authorize
def test_authorize_missing_scope_denied():
    p = Principal(key_id="k", name="n", client_ids=["acme"], scopes=["read"])
    with pytest.raises(mcp_auth.MCPAuthError, match="missing required scope: ingest"):
        mcp_auth.authorize(p, scope="ingest", client_id="acme")


def test_authorize_wrong_tenant_denied():
    p = Principal(key_id="k", name="n", client_ids=["acme"], scopes=["read"])
    with pytest.raises(mcp_auth.MCPAuthError, match="not authorized for client: other"):
        mcp_auth.authorize(p, scope="read", client_id="other")


def test_authorize_ok_is_silent():
    p = Principal(key_id="k", name="n", client_ids=["acme"], scopes=["read", "ingest"])
    mcp_auth.authorize(p, scope="read", client_id="acme")
    mcp_auth.authorize(p, scope="ingest", client_id="acme")


def test_authorize_wildcards():
    p = Principal(key_id="k", name="n", client_ids=["*"], scopes=["*"])
    mcp_auth.authorize(p, scope="admin", client_id="any-tenant")  # must not raise


# --------------------------------------------------------------------------- tool registry
async def test_build_mcp_registers_the_expected_tools():
    from di.mcp import build_mcp

    tools = {t.name for t in await build_mcp().list_tools()}
    expected = {
        "search_knowledge", "get_client_facts", "get_document_tree", "list_client_documents",
        "get_document_manifest", "get_answerable_questions", "get_node_provenance",
        "get_job_status", "submit_ingest",
    }
    assert expected <= tools, f"missing tools: {expected - tools}"


async def test_build_mcp_excludes_admin_and_destructive_tools():
    from di.mcp import build_mcp

    tools = {t.name for t in await build_mcp().list_tools()}
    forbidden = {"adjudicate", "purge_client", "delete_document", "create_key", "revoke_key",
                 "rotate_key", "clear_adjudication"}
    assert not (tools & forbidden), f"destructive tools must not be exposed over MCP: {tools & forbidden}"
