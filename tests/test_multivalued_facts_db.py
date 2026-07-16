"""Live-DB integration tests for the multi-valued-facts store layer (migration 008).

Marked ``integration`` — set ``DI_RUN_INTEGRATION=1`` with a reachable Postgres to run. Covers what
the pure merge-logic tests cannot: the real unique-index/constraint shape after 008, the full-set
replace transaction (including the empty-set-deletes-all contract), the per-client advisory-lock
serialization that prevents a lost-update race between two concurrent re-merges, and the
adjudication event/live-row split (upsert_adjudication / clear_adjudication /
fetch_adjudication_events).
"""
from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest

from di.db import close_pool, init_pool, run_migrations
from di.models import ClientFact

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _setup():
    try:
        await init_pool()
        await run_migrations()
    except Exception as e:  # noqa: BLE001 - any connect/auth/DDL failure -> skip, not fail
        pytest.skip(f"Postgres unavailable/unauthorized: {e}")


@pytest.fixture(autouse=True)
async def _pool():
    await _setup()
    yield
    await close_pool()


def _cid() -> str:
    return f"test-mvf-{uuid.uuid4().hex[:8]}"


def _fact(client_id: str, attribute_key: str, instance_key: str = "", *,
         resolved_value: str = "v", confidence: float = 0.9) -> ClientFact:
    return ClientFact(client_id=client_id, attribute_key=attribute_key, instance_key=instance_key,
                      resolved_value=resolved_value, confidence=confidence,
                      source_fact_ids=[str(uuid.uuid4())])


# ---------------------------------------------------------------------------
# Schema shape after 008
# ---------------------------------------------------------------------------
async def test_new_unique_indexes_exist_and_old_constraints_gone():
    from di.config import get_settings

    s = get_settings()
    from di.db import init_pool as _init

    pool = await _init()
    async with pool.acquire() as conn:
        cmf_indexes = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND tablename = "
            "'client_merged_fact'", s.pg_schema)
        adj_indexes = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND tablename = "
            "'di_fact_adjudication'", s.pg_schema)
        ledger = await conn.fetchval(
            f'SELECT count(*) FROM "{s.pg_schema}".di_migration_ledger '
            "WHERE filename = '008_multi_valued_facts.sql'")
    cmf_names = {r["indexname"] for r in cmf_indexes}
    adj_names = {r["indexname"] for r in adj_indexes}
    assert "client_merged_fact_client_attr_instance" in cmf_names
    assert "client_merged_fact_client_id_attribute_key_key" not in cmf_names
    assert "di_fact_adjudication_client_attr_instance" in adj_names
    assert "di_fact_adjudication_client_id_attribute_key_key" not in adj_names
    assert ledger == 1


async def test_adjudication_event_table_is_append_only():
    from di.config import get_settings
    from di.db import init_pool as _init

    s = get_settings()
    cid = _cid()
    pool = await _init()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT to_regclass($1)", f'"{s.pg_schema}"."di_fact_adjudication_event"')
        assert exists is not None
        # di_owner is deliberately NOT BYPASSRLS (Phase-1 role split) — bind the tenant GUC before
        # writing, same as di.db.acquire(client_id) does for the app's own connections.
        await conn.execute("SELECT set_config('app.current_client_id', $1, false)", cid)
        await conn.execute(
            f'INSERT INTO "{s.pg_schema}".di_fact_adjudication_event '
            "(client_id, attribute_key, instance_key, verdict) VALUES ($1, 'k', '', 'accept')", cid)
        with pytest.raises(asyncpg.RaiseError, match="append-only"):
            await conn.execute(
                f'UPDATE "{s.pg_schema}".di_fact_adjudication_event SET verdict = $1 '
                "WHERE client_id = $2", "reject", cid)


# ---------------------------------------------------------------------------
# replace_merged_facts: full-set replace contract
# ---------------------------------------------------------------------------
async def test_replace_merged_facts_stale_delete_removes_dropped_instances():
    from di import store

    cid = _cid()
    a = _fact(cid, "ownership.director", "aaaa", resolved_value="Juan")
    b = _fact(cid, "ownership.director", "bbbb", resolved_value="Maria")
    await store.replace_merged_facts(cid, [a, b])
    rows = await store.fetch_merged_facts(cid)
    assert {r["instance_key"] for r in rows} == {"aaaa", "bbbb"}

    # Second pass drops 'bbbb' (e.g. that director's source document was deleted)
    await store.replace_merged_facts(cid, [a])
    rows = await store.fetch_merged_facts(cid)
    assert {r["instance_key"] for r in rows} == {"aaaa"}


async def test_replace_merged_facts_empty_set_deletes_all_rows():
    """The corrected-design contract: an empty facts list is NOT a no-op — it deletes every
    merged row for the client (e.g. after the client's last document is deleted)."""
    from di import store

    cid = _cid()
    await store.replace_merged_facts(cid, [_fact(cid, "id.ssn", resolved_value="123")])
    assert len(await store.fetch_merged_facts(cid)) == 1

    await store.replace_merged_facts(cid, [])
    assert await store.fetch_merged_facts(cid) == []


async def test_replace_merged_facts_upserts_in_place():
    from di import store

    cid = _cid()
    await store.replace_merged_facts(cid, [_fact(cid, "id.ssn", resolved_value="111")])
    await store.replace_merged_facts(cid, [_fact(cid, "id.ssn", resolved_value="222")])
    rows = await store.fetch_merged_facts(cid)
    assert len(rows) == 1
    assert rows[0]["resolved_value"] == "222"


async def test_concurrent_double_remerge_no_lost_rows():
    """Corrected-design test #11: assert NO LOST ROWS after two concurrent re-merges of the SAME
    client, not merely 'no unique violation'. The per-client advisory xact lock in
    replace_merged_facts serializes the two full-set replaces; both write the SAME final set here
    (as a real concurrent re-merge from identical source facts would), so convergence means the
    final row set equals that set exactly — nothing dropped by a lost-update race.
    """
    from di import store

    cid = _cid()
    facts = [
        _fact(cid, "ownership.director", "aaaa", resolved_value="Juan"),
        _fact(cid, "ownership.director", "bbbb", resolved_value="Maria"),
        _fact(cid, "ownership.director", "cccc", resolved_value="Carlos"),
    ]
    await asyncio.gather(
        store.replace_merged_facts(cid, facts),
        store.replace_merged_facts(cid, facts),
    )
    rows = await store.fetch_merged_facts(cid)
    assert {r["instance_key"] for r in rows} == {"aaaa", "bbbb", "cccc"}


# ---------------------------------------------------------------------------
# Adjudication: live row + append-only history
# ---------------------------------------------------------------------------
async def test_upsert_adjudication_writes_live_row_and_event():
    from di import store

    cid = _cid()
    await store.upsert_adjudication(cid, attribute_key="ownership.director", instance_key="aaaa",
                                    verdict="reject", reviewer="reviewer-1")
    live = await store.fetch_adjudications(cid)
    assert len(live) == 1 and live[0]["verdict"] == "reject"
    events = await store.fetch_adjudication_events(cid)
    assert len(events) == 1 and events[0]["verdict"] == "reject"


async def test_second_verdict_updates_live_row_without_resetting_created_at():
    from di import store

    cid = _cid()
    await store.upsert_adjudication(cid, attribute_key="ownership.director", instance_key="aaaa",
                                    verdict="reject")
    first = (await store.fetch_adjudications(cid))[0]
    await asyncio.sleep(0.01)
    await store.upsert_adjudication(cid, attribute_key="ownership.director", instance_key="aaaa",
                                    verdict="accept")
    second = (await store.fetch_adjudications(cid))[0]
    assert second["verdict"] == "accept"
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] > first["updated_at"]
    # both verdicts are preserved in the append-only history
    events = await store.fetch_adjudication_events(cid)
    assert [e["verdict"] for e in sorted(events, key=lambda e: e["created_at"])] == \
        ["reject", "accept"]


async def test_clear_adjudication_removes_live_row_and_appends_cleared_event():
    from di import store

    cid = _cid()
    await store.upsert_adjudication(cid, attribute_key="ownership.director", instance_key="aaaa",
                                    verdict="reject")
    cleared = await store.clear_adjudication(cid, attribute_key="ownership.director",
                                             instance_key="aaaa", reviewer="reviewer-2")
    assert cleared is True
    assert await store.fetch_adjudications(cid) == []
    events = await store.fetch_adjudication_events(cid)
    verdicts = [e["verdict"] for e in sorted(events, key=lambda e: e["created_at"])]
    assert verdicts == ["reject", "cleared"]


async def test_clear_adjudication_returns_false_when_nothing_to_clear():
    from di import store

    cid = _cid()
    cleared = await store.clear_adjudication(cid, attribute_key="ownership.director",
                                             instance_key="never-set")
    assert cleared is False
    assert await store.fetch_adjudication_events(cid) == []
