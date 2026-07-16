"""Unit tests for the per-tenant ingest admission quota (di/routers/ingest.py::_enforce_ingest_quota).

Pure-logic: monkeypatches store.fetch_tenant_policy and jobs.count_active_and_today, no DB.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from di import jobs, store
from di.config import get_settings
from di.routers import ingest


def _patch_policy(monkeypatch, policy: dict | None) -> None:
    async def fake(client_id: str):
        return policy

    monkeypatch.setattr(store, "fetch_tenant_policy", fake)


def _patch_counts(monkeypatch, active: int, today: int) -> None:
    async def fake(client_id: str):
        return active, today

    monkeypatch.setattr(jobs, "count_active_and_today", fake)


@pytest.mark.asyncio
async def test_allows_when_under_every_limit(monkeypatch):
    _patch_policy(monkeypatch, None)
    _patch_counts(monkeypatch, active=1, today=1)
    await ingest._enforce_ingest_quota("c1")  # must not raise


@pytest.mark.asyncio
async def test_fleet_default_daily_zero_means_unlimited(monkeypatch):
    """settings.ingest_daily_limit_per_client == 0 means no daily cap — must never 429."""
    assert get_settings().ingest_daily_limit_per_client == 0
    _patch_policy(monkeypatch, None)
    _patch_counts(monkeypatch, active=0, today=10_000)
    await ingest._enforce_ingest_quota("c1")  # must not raise even with a huge daily count


@pytest.mark.asyncio
async def test_active_jobs_limit_trips_429(monkeypatch):
    _patch_policy(monkeypatch, {"max_active_jobs": 5, "daily_ingest_limit": None})
    _patch_counts(monkeypatch, active=5, today=0)
    with pytest.raises(HTTPException) as ei:
        await ingest._enforce_ingest_quota("c1")
    assert ei.value.status_code == 429
    assert "active jobs" in ei.value.detail


@pytest.mark.asyncio
async def test_tenant_policy_daily_limit_trips_429(monkeypatch):
    _patch_policy(monkeypatch, {"max_active_jobs": None, "daily_ingest_limit": 3})
    _patch_counts(monkeypatch, active=0, today=3)
    with pytest.raises(HTTPException) as ei:
        await ingest._enforce_ingest_quota("c1")
    assert ei.value.status_code == 429
    assert "daily ingest quota" in ei.value.detail


@pytest.mark.asyncio
async def test_tenant_policy_daily_limit_zero_blocks_entirely(monkeypatch):
    """An explicit per-tenant override of 0 is a deliberate 'block this tenant' lever — distinct
    from the fleet default's 0 (unlimited). See TenantPolicyRequest.daily_ingest_limit."""
    _patch_policy(monkeypatch, {"max_active_jobs": None, "daily_ingest_limit": 0})
    _patch_counts(monkeypatch, active=0, today=0)
    with pytest.raises(HTTPException) as ei:
        await ingest._enforce_ingest_quota("c1")
    assert ei.value.status_code == 429


@pytest.mark.asyncio
async def test_tenant_policy_overrides_fleet_default_upward(monkeypatch):
    _patch_policy(monkeypatch, {"max_active_jobs": 500, "daily_ingest_limit": None})
    _patch_counts(monkeypatch, active=100, today=0)
    await ingest._enforce_ingest_quota("c1")  # must not raise: well under the raised override


@pytest.mark.asyncio
async def test_inflight_streams_count_toward_active_limit(monkeypatch):
    _patch_policy(monkeypatch, {"max_active_jobs": 2, "daily_ingest_limit": None})
    _patch_counts(monkeypatch, active=1, today=0)
    ingest._inflight_streams["c1"] = 1
    try:
        with pytest.raises(HTTPException) as ei:
            await ingest._enforce_ingest_quota("c1")
        assert ei.value.status_code == 429
    finally:
        ingest._inflight_streams.pop("c1", None)


@pytest.mark.asyncio
async def test_policy_lookup_failure_falls_back_to_fleet_defaults(monkeypatch):
    async def boom(client_id: str):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(store, "fetch_tenant_policy", boom)
    _patch_counts(monkeypatch, active=0, today=0)
    await ingest._enforce_ingest_quota("c1")  # must not raise: quota admission is best-effort
