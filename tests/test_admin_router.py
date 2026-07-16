"""Router wiring tests for the admin adjudication endpoints — call the coroutines directly with
monkeypatched store, mirroring tests/test_routers.py's pattern. No DB, no network.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from di import store
from di.auth import Principal
from di.routers import admin


def _principal(client_ids: list[str] | None = None) -> Principal:
    return Principal(key_id="test-key", name="reviewer-1", client_ids=client_ids or ["*"],
                     scopes=["admin"])


@pytest.mark.asyncio
async def test_adjudicate_multi_key_without_instance_key_is_422(monkeypatch):
    body = admin.AdjudicationRequest(attribute_key="ownership.director", instance_key="",
                                     verdict="accept")
    with pytest.raises(HTTPException) as ei:
        await admin.adjudicate("c1", body, principal=_principal())
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_adjudicate_single_key_with_instance_key_is_422(monkeypatch):
    body = admin.AdjudicationRequest(attribute_key="identity.full_name", instance_key="aaaa",
                                     verdict="accept")
    with pytest.raises(HTTPException) as ei:
        await admin.adjudicate("c1", body, principal=_principal())
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_adjudicate_multi_key_nonexistent_instance_is_404(monkeypatch):
    async def fake_merged(client_id, *, attribute_key=None):
        return []

    async def fake_adjudications(client_id):
        return []

    monkeypatch.setattr(store, "fetch_merged_facts", fake_merged)
    monkeypatch.setattr(store, "fetch_adjudications", fake_adjudications)
    body = admin.AdjudicationRequest(attribute_key="ownership.director", instance_key="deadbeef",
                                     verdict="reject")
    with pytest.raises(HTTPException) as ei:
        await admin.adjudicate("c1", body, principal=_principal())
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_adjudicate_multi_key_existing_merged_instance_succeeds(monkeypatch):
    async def fake_merged(client_id, *, attribute_key=None):
        return [{"attribute_key": "ownership.director", "instance_key": "aaaa"}]

    async def fake_adjudications(client_id):
        return []

    calls = {}

    async def fake_upsert(client_id, **kw):
        calls.update(kw)

    async def fake_remerge(client_id):
        return 3

    monkeypatch.setattr(store, "fetch_merged_facts", fake_merged)
    monkeypatch.setattr(store, "fetch_adjudications", fake_adjudications)
    monkeypatch.setattr(store, "upsert_adjudication", fake_upsert)
    monkeypatch.setattr(admin, "_remerge_client_facts", fake_remerge)

    body = admin.AdjudicationRequest(attribute_key="ownership.director", instance_key="aaaa",
                                     verdict="reject")
    res = await admin.adjudicate("c1", body, principal=_principal())
    assert res["instance_key"] == "aaaa"
    assert calls["instance_key"] == "aaaa"


@pytest.mark.asyncio
async def test_adjudicate_multi_key_instance_only_in_adjudication_row_still_succeeds(monkeypatch):
    """Fix for the one-way-door bug: a rejected instance's merged row is gone, but its
    adjudication row still exists — that must also satisfy the existence check (so a reviewer can
    re-adjudicate a previously-rejected instance without a permanent 404)."""
    async def fake_merged(client_id, *, attribute_key=None):
        return []  # rejected -> no merged row

    async def fake_adjudications(client_id):
        return [{"attribute_key": "ownership.director", "instance_key": "aaaa", "verdict": "reject"}]

    async def fake_upsert(client_id, **kw):
        pass

    async def fake_remerge(client_id):
        return 1

    monkeypatch.setattr(store, "fetch_merged_facts", fake_merged)
    monkeypatch.setattr(store, "fetch_adjudications", fake_adjudications)
    monkeypatch.setattr(store, "upsert_adjudication", fake_upsert)
    monkeypatch.setattr(admin, "_remerge_client_facts", fake_remerge)

    body = admin.AdjudicationRequest(attribute_key="ownership.director", instance_key="aaaa",
                                     verdict="accept")
    res = await admin.adjudicate("c1", body, principal=_principal())
    assert res["verdict"] == "accept"


@pytest.mark.asyncio
async def test_adjudicate_single_key_unchanged(monkeypatch):
    """Single-valued adjudicate never runs the existence check at all — untouched behavior."""
    calls = {}

    async def fake_upsert(client_id, **kw):
        calls.update(kw)

    async def fake_remerge(client_id):
        return 1

    monkeypatch.setattr(store, "upsert_adjudication", fake_upsert)
    monkeypatch.setattr(admin, "_remerge_client_facts", fake_remerge)

    body = admin.AdjudicationRequest(attribute_key="identity.full_name", verdict="accept")
    res = await admin.adjudicate("c1", body, principal=_principal())
    assert res["instance_key"] == ""
    assert calls["instance_key"] == ""


@pytest.mark.asyncio
async def test_list_adjudications_denies_other_tenants(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        await admin.list_adjudications("other-client", principal=_principal(client_ids=["c1"]))
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_list_adjudications_returns_store_rows(monkeypatch):
    async def fake(client_id):
        return [{"attribute_key": "ownership.director", "instance_key": "aaaa", "verdict": "reject"}]

    monkeypatch.setattr(store, "fetch_adjudications", fake)
    res = await admin.list_adjudications("c1", principal=_principal())
    assert len(res) == 1 and res[0]["verdict"] == "reject"


@pytest.mark.asyncio
async def test_adjudication_history_passes_filters_through(monkeypatch):
    captured = {}

    async def fake(client_id, *, attribute_key=None, instance_key=None):
        captured["attribute_key"] = attribute_key
        captured["instance_key"] = instance_key
        return []

    monkeypatch.setattr(store, "fetch_adjudication_events", fake)
    await admin.adjudication_history("c1", attribute_key="ownership.director",
                                     instance_key="aaaa", principal=_principal())
    assert captured == {"attribute_key": "ownership.director", "instance_key": "aaaa"}


@pytest.mark.asyncio
async def test_clear_adjudication_remerges_when_cleared(monkeypatch):
    async def fake_clear(client_id, **kw):
        return True

    async def fake_remerge(client_id):
        return 2

    monkeypatch.setattr(store, "clear_adjudication", fake_clear)
    monkeypatch.setattr(admin, "_remerge_client_facts", fake_remerge)
    res = await admin.clear_adjudication("c1", "ownership.director", instance_key="aaaa",
                                         principal=_principal())
    assert res.cleared is True
    assert res.remerged_facts == 2


@pytest.mark.asyncio
async def test_clear_adjudication_no_op_when_nothing_to_clear(monkeypatch):
    async def fake_clear(client_id, **kw):
        return False

    remerge_called = False

    async def fake_remerge(client_id):
        nonlocal remerge_called
        remerge_called = True
        return 0

    monkeypatch.setattr(store, "clear_adjudication", fake_clear)
    monkeypatch.setattr(admin, "_remerge_client_facts", fake_remerge)
    res = await admin.clear_adjudication("c1", "ownership.director", principal=_principal())
    assert res.cleared is False
    assert res.remerged_facts is None
    assert remerge_called is False
