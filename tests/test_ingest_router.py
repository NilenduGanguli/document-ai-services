"""Unit tests for di.routers.ingest's accept-path guards.

Pure-logic — no DB, no network. The full accept path (blob-put + enqueue) is exercised live in
tools/smoke_test.py and the DI_RUN_INTEGRATION suite; this file covers the standalone checks.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from di.config import get_settings
from di.routers import ingest


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_blob_backend_none_rejects_async_ingest(monkeypatch):
    monkeypatch.setenv("BLOB_BACKEND", "none")
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as ei:
        ingest._require_durable_blob_backend()
    assert ei.value.status_code == 503
    assert "BLOB_BACKEND=none" in ei.value.detail


@pytest.mark.parametrize("backend", ["postgres", "local", "s3"])
def test_durable_blob_backends_are_accepted(monkeypatch, backend):
    monkeypatch.setenv("BLOB_BACKEND", backend)
    get_settings.cache_clear()
    ingest._require_durable_blob_backend()  # must not raise
