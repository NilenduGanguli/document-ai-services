"""Shared test fixtures + capability gating.

Pure-logic tests run anywhere. DB tests require a live Postgres (with ltree); pgvector tests are
skipped when the extension is absent; ml/network tests are skipped without their deps/services.
Default to the in-process retrieval stub so nothing reaches the network.
"""
from __future__ import annotations

import os

# Force offline model gateway BEFORE di.config is imported/cached.
os.environ.setdefault("DI_RETRIEVAL_STUB", "true")
os.environ.setdefault("RLS_ENABLED", "false")

import pytest  # noqa: E402

from di.config import get_settings  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def retrieval_stub():
    from di.retrieval_client import StubRetrievalClient

    return StubRetrievalClient(get_settings())


def _postgres_reachable() -> bool:
    import socket

    s = get_settings()
    try:
        with socket.create_connection((s.pg_host, s.pg_port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def db_available() -> bool:
    return _postgres_reachable()
