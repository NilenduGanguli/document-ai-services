"""Auth tests — hashing, key generation, grant evaluation, and the FastAPI dependencies.

Pure-logic (no DB / no network): ``get_settings`` is monkeypatched and ``acquire`` is booby-trapped
so any accidental DB round-trip fails loudly. The live-DB round-trip is marked ``integration`` and
skips cleanly when Postgres (or migration 005) is unavailable.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from di import auth
from di.auth import (
    API_KEY_HEADER,
    KEY_PREFIX,
    Principal,
    authorize_client,
    generate_key,
    hash_key,
    require_principal,
    require_scope,
    reset_auth_cache,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_auth_cache()
    yield
    reset_auth_cache()


def _request(headers: dict[str, str] | None = None) -> Request:
    """Build a minimal ASGI request carrying ``headers``."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


def _stub_settings(monkeypatch: pytest.MonkeyPatch, **kwargs) -> None:
    """Point di.auth at stub settings and make any DB access an error."""
    defaults = {"auth_enabled": True, "di_bootstrap_api_key": "", "pg_schema": "di"}
    settings = SimpleNamespace(**{**defaults, **kwargs})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)

    def _no_db(*args, **kw):
        raise AssertionError("auth must not touch the database on this path")

    monkeypatch.setattr(auth, "acquire", _no_db)


# ---------------------------------------------------------------------------
# hash_key / generate_key
# ---------------------------------------------------------------------------
def test_hash_key_is_deterministic_sha256():
    # sha256("di_test") — pinned so a hashing change is caught, not silently accepted.
    expected = "228c4ef9477eb250fffc5659610f1ae516d74c866ac4f3d27ea68d44ee978cf8"
    assert hash_key("di_test") == hash_key("di_test")
    assert len(hash_key("di_test")) == 64
    assert hash_key("di_test") != hash_key("di_test2")
    assert hash_key("di_test") == expected


def test_hash_key_never_contains_the_raw_key():
    raw = generate_key()
    assert raw not in hash_key(raw)


def test_generate_key_format_and_uniqueness():
    key = generate_key()
    assert key.startswith(KEY_PREFIX)
    body = key[len(KEY_PREFIX):]
    assert len(body) >= 43  # token_urlsafe(32) -> 43 chars
    assert set(body) <= set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    )
    assert len({generate_key() for _ in range(200)}) == 200


# ---------------------------------------------------------------------------
# Principal grants
# ---------------------------------------------------------------------------
def test_can_access_wildcard_grants_every_tenant():
    p = Principal(key_id="k", name="n", client_ids=["*"], scopes=[])
    assert p.can_access("acme")
    assert p.can_access("anything-else")


def test_can_access_exact_match_and_deny():
    p = Principal(key_id="k", name="n", client_ids=["acme", "globex"], scopes=[])
    assert p.can_access("acme")
    assert p.can_access("globex")
    assert not p.can_access("initech")
    assert not p.can_access("")
    assert not p.can_access("*")  # a literal "*" tenant must not match a non-wildcard grant


def test_can_access_empty_grant_denies_all():
    p = Principal(key_id="k", name="n", client_ids=[], scopes=["*"])
    assert not p.can_access("acme")


def test_has_scope_wildcard_exact_and_deny():
    wild = Principal(key_id="k", name="n", client_ids=[], scopes=["*"])
    assert wild.has_scope("ingest") and wild.has_scope("read") and wild.has_scope("admin")

    scoped = Principal(key_id="k", name="n", client_ids=[], scopes=["read"])
    assert scoped.has_scope("read")
    assert not scoped.has_scope("ingest")
    assert not scoped.has_scope("admin")

    none = Principal(key_id="k", name="n", client_ids=[], scopes=[])
    assert not none.has_scope("read")


def test_scopes_and_client_ids_are_independent():
    """A tenant grant must not imply a scope grant, nor the reverse."""
    p = Principal(key_id="k", name="n", client_ids=["*"], scopes=["read"])
    assert p.can_access("acme")
    assert not p.has_scope("admin")


# ---------------------------------------------------------------------------
# require_principal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_auth_disabled_yields_wildcard_principal_without_db(monkeypatch):
    _stub_settings(monkeypatch, auth_enabled=False)
    principal = await require_principal(_request())
    assert principal.key_id == "local-dev"
    assert principal.name == "local-dev"
    assert principal.client_ids == ["*"]
    assert principal.scopes == ["*"]
    assert principal.can_access("any-tenant")
    assert principal.has_scope("admin")


@pytest.mark.asyncio
async def test_auth_disabled_ignores_any_supplied_key(monkeypatch):
    _stub_settings(monkeypatch, auth_enabled=False)
    principal = await require_principal(_request({API_KEY_HEADER: "garbage"}))
    assert principal.client_ids == ["*"]


@pytest.mark.asyncio
async def test_missing_header_is_401_without_db(monkeypatch):
    from fastapi import HTTPException

    _stub_settings(monkeypatch, auth_enabled=True)
    with pytest.raises(HTTPException) as exc:
        await require_principal(_request())
    assert exc.value.status_code == 401
    assert "WWW-Authenticate" in exc.value.headers


@pytest.mark.asyncio
async def test_invalid_key_is_401_and_never_echoes_the_key(monkeypatch):
    from fastapi import HTTPException

    _stub_settings(monkeypatch, auth_enabled=True)

    async def _unknown(raw_key: str):
        return None

    monkeypatch.setattr(auth, "resolve_principal", _unknown)
    secret = "di_super-secret-key"
    with pytest.raises(HTTPException) as exc:
        await require_principal(_request({API_KEY_HEADER: secret}))
    assert exc.value.status_code == 401
    assert secret not in str(exc.value.detail)
    assert secret not in str(exc.value.headers)


@pytest.mark.asyncio
async def test_empty_raw_key_resolves_to_none_without_db(monkeypatch):
    _stub_settings(monkeypatch, auth_enabled=True)
    assert await auth.resolve_principal("") is None


# ---------------------------------------------------------------------------
# require_scope / authorize_client
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_require_scope_allows_holder_and_wildcard():
    dep = require_scope("ingest")
    holder = Principal(key_id="k", name="n", client_ids=["*"], scopes=["ingest"])
    wild = Principal(key_id="k", name="n", client_ids=["*"], scopes=["*"])
    assert await dep(holder) is holder
    assert await dep(wild) is wild


@pytest.mark.asyncio
async def test_require_scope_403_when_missing():
    from fastapi import HTTPException

    dep = require_scope("admin")
    principal = Principal(key_id="k", name="n", client_ids=["*"], scopes=["read"])
    with pytest.raises(HTTPException) as exc:
        await dep(principal)
    assert exc.value.status_code == 403
    assert "admin" in exc.value.detail


def test_authorize_client_permits_and_denies():
    from fastapi import HTTPException

    p = Principal(key_id="k", name="n", client_ids=["acme"], scopes=["*"])
    authorize_client(p, "acme")  # must not raise
    with pytest.raises(HTTPException) as exc:
        authorize_client(p, "globex")
    assert exc.value.status_code == 403


def test_authorize_client_wildcard_permits_any():
    p = Principal(key_id="k", name="n", client_ids=["*"], scopes=[])
    authorize_client(p, "whatever")  # must not raise


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cache_hit_avoids_db_and_reset_clears_it(monkeypatch):
    """A memoized principal is served without a DB round-trip; reset_auth_cache drops it."""
    _stub_settings(monkeypatch, auth_enabled=True)  # acquire() would raise if reached
    raw = "di_cached-key"
    principal = Principal(key_id="k1", name="n", client_ids=["acme"], scopes=["read"])
    auth._cache[hash_key(raw)] = (auth.time.monotonic() + 30.0, principal)

    assert await auth.resolve_principal(raw) is principal

    reset_auth_cache()
    with pytest.raises(AssertionError):  # cache empty -> falls through to the booby-trapped DB
        await auth.resolve_principal(raw)


@pytest.mark.asyncio
async def test_expired_cache_entry_is_not_served(monkeypatch):
    _stub_settings(monkeypatch, auth_enabled=True)
    raw = "di_stale-key"
    principal = Principal(key_id="k1", name="n", client_ids=["acme"], scopes=["read"])
    auth._cache[hash_key(raw)] = (auth.time.monotonic() - 1.0, principal)  # already expired
    with pytest.raises(AssertionError):  # must re-check the DB rather than serve a stale grant
        await auth.resolve_principal(raw)


@pytest.mark.asyncio
async def test_revoke_rejects_malformed_uuid_without_db(monkeypatch):
    _stub_settings(monkeypatch, auth_enabled=True)
    assert await auth.revoke_api_key("not-a-uuid") is False


# ---------------------------------------------------------------------------
# Live DB round-trip
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
async def test_api_key_lifecycle_round_trip():
    """create -> resolve -> list -> revoke against a live di_api_key table."""
    from di.db import close_pool, init_pool, run_migrations

    try:
        await init_pool()
        await run_migrations()
    except Exception as e:  # noqa: BLE001 - any connect/auth/DDL failure -> skip, not fail
        pytest.skip(f"Postgres unavailable/unauthorized: {e}")

    try:
        cid = f"test-{uuid.uuid4().hex[:8]}"
        try:
            key_id, raw = await auth.create_api_key(
                name=f"test-{cid}", client_ids=[cid], scopes=["read"]
            )
        except Exception as e:  # noqa: BLE001 - di_api_key ships in migration 005
            pytest.skip(f"di_api_key table unavailable: {e}")

        assert raw.startswith(KEY_PREFIX)

        principal = await auth.resolve_principal(raw)
        assert principal is not None
        assert principal.key_id == key_id
        assert principal.can_access(cid)
        assert not principal.can_access("other-tenant")
        assert principal.has_scope("read")
        assert not principal.has_scope("admin")

        listed = [k for k in await auth.list_api_keys() if k["id"] == key_id]
        assert len(listed) == 1
        assert "key_hash" not in listed[0]
        assert raw not in str(listed[0])

        assert await auth.revoke_api_key(key_id) is True
        reset_auth_cache()
        assert await auth.resolve_principal(raw) is None
        assert await auth.revoke_api_key(key_id) is False  # already disabled
    finally:
        reset_auth_cache()
        await close_pool()
