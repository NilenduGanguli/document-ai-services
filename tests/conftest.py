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


@pytest.fixture(autouse=True)
async def _isolate_db_pool(request: pytest.FixtureRequest):
    """Give every integration test its own asyncpg pool.

    ``di.db`` caches one pool globally, but pytest-asyncio runs each test on a fresh event loop —
    reusing a pool bound to a closed loop raises "another operation is in progress" /
    "Event loop is closed". Dropping the pool around each test keeps them independent. This is a
    test-harness concern only: the app has a single long-lived loop.
    """
    if "integration" not in request.keywords:
        yield
        return
    from di import db

    await db.close_pool()
    try:
        yield
    finally:
        await db.close_pool()


def pytest_collection_modifyitems(config: pytest.Config,
                                  items: list[pytest.Item]) -> None:
    """Skip ``@pytest.mark.integration`` tests unless explicitly opted in.

    The marker alone does not skip. Gating on "is a port open" is not enough — an unrelated
    Postgres on :5432 makes these run against the wrong database — so require an explicit
    DI_RUN_INTEGRATION=1 plus a DB that actually answers.
    """
    if os.environ.get("DI_RUN_INTEGRATION") == "1" and _postgres_reachable():
        return
    skip = pytest.mark.skip(
        reason="integration test: set DI_RUN_INTEGRATION=1 with a live Postgres "
               "(docker compose up) to run"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
