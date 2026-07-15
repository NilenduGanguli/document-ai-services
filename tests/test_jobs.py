"""Job store tests — cursor codec, limit clamping, and model contracts.

Pure-logic (no DB / no network) except the ``integration``-marked round-trip at the bottom, which
skips cleanly when Postgres (or migration 005) is unavailable. The cursor codec is the interesting
surface here: cursors are client-supplied and every malformed shape must raise ValueError rather
than reaching the driver as a bad parameter.
"""
from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from di.jobs import (
    Job,
    JobEvent,
    JobStatus,
    _clamp_limit,
    _decode_cursor,
    _encode_cursor,
    _rowcount,
)


def _b64(text: str) -> str:
    """Build a structurally valid cursor with arbitrary (possibly bad) payload."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# ---------------------------------------------------------------------------
# Cursor codec
# ---------------------------------------------------------------------------
def test_encode_decode_cursor_round_trip():
    ts = datetime(2026, 7, 15, 12, 30, 45, 123456, tzinfo=UTC)
    job_id = str(uuid.uuid4())

    cursor = _encode_cursor(ts, job_id)
    got_ts, got_id = _decode_cursor(cursor)

    assert got_ts == ts
    assert got_id == job_id


def test_cursor_is_opaque_and_url_safe():
    """Cursors must be URL-safe and must not leak the raw keyset position verbatim."""
    ts = datetime(2026, 7, 15, 12, 30, 45, tzinfo=UTC)
    job_id = str(uuid.uuid4())

    cursor = _encode_cursor(ts, job_id)

    assert job_id not in cursor
    assert "|" not in cursor
    assert "=" not in cursor  # padding stripped
    assert "+" not in cursor and "/" not in cursor  # url-safe alphabet only


def test_cursor_round_trip_preserves_microseconds_and_offset():
    """Sub-second precision must survive: it is the primary sort key's tie-breaker."""
    ts = datetime(2026, 1, 2, 3, 4, 5, 987654, tzinfo=UTC)
    job_id = str(uuid.uuid4())

    got_ts, _ = _decode_cursor(_encode_cursor(ts, job_id))

    assert got_ts.microsecond == 987654
    assert got_ts.tzinfo is not None
    assert got_ts.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "bad, reason",
    [
        ("", "empty"),
        ("!!!!", "not base64"),
        ("§§§§", "non-ascii"),
        (_b64("no-separator-here"), "missing | separator"),
        (_b64("|"), "empty timestamp and id"),
        (_b64(f"not-a-timestamp|{uuid.uuid4()}"), "unparseable timestamp"),
        (_b64("2026-13-45T99:99:99|" + str(uuid.uuid4())), "out-of-range timestamp"),
        (_b64("2026-07-15T12:00:00+00:00|not-a-uuid"), "id is not a uuid"),
        (_b64("2026-07-15T12:00:00+00:00|"), "missing id"),
        (base64.urlsafe_b64encode(b"\xff\xfe\xfd\xfc").decode().rstrip("="), "non-utf8 payload"),
    ],
)
def test_decode_cursor_rejects_malformed(bad, reason):
    with pytest.raises(ValueError):
        _decode_cursor(bad)


def test_decode_cursor_error_does_not_leak_stack_of_driver():
    """A bad cursor surfaces as ValueError mentioning the cursor — 400-mappable, not a 500."""
    with pytest.raises(ValueError, match="malformed cursor"):
        _decode_cursor("!!!!")


# ---------------------------------------------------------------------------
# Limit clamping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "given, expected",
    [
        (50, 50),      # default, untouched
        (1, 1),        # lower bound
        (200, 200),    # upper bound
        (0, 1),        # below range
        (-10, 1),      # negative
        (201, 200),    # just over
        (10_000, 200), # far over
    ],
)
def test_clamp_limit(given, expected):
    assert _clamp_limit(given) == expected


# ---------------------------------------------------------------------------
# Command-tag parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tag, expected",
    [("DELETE 3", 3), ("DELETE 0", 0), ("UPDATE 12", 12), ("", 0), ("DELETE", 0)],
)
def test_rowcount_parses_command_tag(tag, expected):
    assert _rowcount(tag) == expected


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
def test_job_status_values():
    assert JobStatus.queued.value == "queued"
    assert JobStatus.running.value == "running"
    assert JobStatus.succeeded.value == "succeeded"
    assert JobStatus.failed.value == "failed"
    assert JobStatus("failed") is JobStatus.failed


def test_job_event_defaults():
    ev = JobEvent(stage="ocr")

    assert ev.status == "ok"
    assert ev.detail == {}
    assert ev.ts.tzinfo is not None, "ts must be tz-aware (stored in a timestamptz column)"


def test_job_event_default_detail_is_not_shared():
    """default_factory (not a shared mutable default) — one event's detail must not bleed."""
    a, b = JobEvent(stage="ocr"), JobEvent(stage="gate")
    a.detail["pages"] = 3

    assert b.detail == {}


def test_job_event_json_round_trip():
    """Mirrors the persistence path: model_dump(mode="json") -> jsonb -> model_validate."""
    ev = JobEvent(stage="gate", status="error", detail={"reason": "pii", "count": 2})

    restored = JobEvent.model_validate(ev.model_dump(mode="json"))

    assert restored == ev
    assert restored.ts == ev.ts


def test_job_event_dump_is_jsonb_safe():
    ev = JobEvent(stage="ocr")
    dumped = ev.model_dump(mode="json")

    assert isinstance(dumped["ts"], str), "ts must serialize to a string for jsonb"
    assert set(dumped) == {"stage", "status", "detail", "ts"}


def test_job_defaults():
    now = datetime.now(tz=UTC)
    job = Job(id=str(uuid.uuid4()), client_id="c1", status=JobStatus.queued,
              created_at=now, updated_at=now)

    assert job.stage is None
    assert job.document_name is None
    assert job.doc_id is None
    assert job.version_id is None
    assert job.error is None
    assert job.idempotency_key is None
    assert job.events == []
    assert job.finished_at is None


def test_job_json_round_trip_with_events():
    now = datetime.now(tz=UTC)
    job = Job(
        id=str(uuid.uuid4()), client_id="c1", status=JobStatus.running, stage="ocr",
        document_name="passport.pdf", events=[JobEvent(stage="upload"), JobEvent(stage="ocr")],
        created_at=now, updated_at=now,
    )

    restored = Job.model_validate_json(job.model_dump_json())

    assert restored == job
    assert [e.stage for e in restored.events] == ["upload", "ocr"]
    assert restored.status is JobStatus.running


def test_job_events_parse_from_raw_jsonb_shape():
    """Rows come back from asyncpg's jsonb codec as plain dicts — Job must absorb that shape."""
    now = datetime.now(tz=UTC)
    job = Job(
        id=str(uuid.uuid4()), client_id="c1", status=JobStatus.succeeded,
        events=[{"stage": "ocr", "status": "ok", "detail": {}, "ts": "2026-07-15T12:00:00+00:00"}],
        created_at=now, updated_at=now, finished_at=now,
    )

    assert isinstance(job.events[0], JobEvent)
    assert job.events[0].stage == "ocr"


# ---------------------------------------------------------------------------
# Live-DB round-trip
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
async def test_job_store_round_trip():
    """Create -> append events -> terminal status -> keyset page -> purge, against a real DB."""
    import asyncpg

    from di import jobs
    from di.db import close_pool, init_pool, run_migrations

    try:
        await init_pool()
        await run_migrations()
    except Exception as e:  # noqa: BLE001 - any connect/auth/DDL failure -> skip, not fail
        pytest.skip(f"Postgres unavailable/unauthorized: {e}")

    cid = f"test-{uuid.uuid4().hex[:8]}"
    try:
        try:
            job = await jobs.create_job(client_id=cid, document_name="passport.pdf")
        except asyncpg.UndefinedTableError:
            pytest.skip("migration 005 (di_job) not applied yet")

        assert job.status is JobStatus.queued
        assert job.finished_at is None

        # concurrent-safe append: two events land, neither clobbers the other
        await jobs.append_event(cid, job.id, JobEvent(stage="upload"))
        await jobs.append_event(cid, job.id, JobEvent(stage="ocr", detail={"pages": 2}))
        fetched = await jobs.get_job(cid, job.id)
        assert fetched is not None
        assert [e.stage for e in fetched.events] == ["upload", "ocr"]
        assert fetched.events[1].detail == {"pages": 2}

        # terminal status stamps finished_at
        await jobs.set_status(cid, job.id, JobStatus.succeeded, stage="done")
        done = await jobs.get_job(cid, job.id)
        assert done is not None
        assert done.status is JobStatus.succeeded
        assert done.stage == "done"
        assert done.finished_at is not None

        # idempotency: same key collapses onto one job
        key = f"idem-{uuid.uuid4().hex[:8]}"
        first = await jobs.create_job(client_id=cid, document_name="a.pdf", idempotency_key=key)
        second = await jobs.create_job(client_id=cid, document_name="a.pdf", idempotency_key=key)
        assert first.id == second.id
        found = await jobs.find_by_idempotency(cid, key)
        assert found is not None and found.id == first.id

        # keyset pagination walks every job exactly once, newest first
        page1, cursor1 = await jobs.list_jobs(cid, limit=1)
        assert len(page1) == 1 and cursor1 is not None
        page2, _ = await jobs.list_jobs(cid, limit=1, cursor=cursor1)
        assert len(page2) == 1
        assert page1[0].id != page2[0].id
        assert page1[0].created_at >= page2[0].created_at

        # status filter
        succeeded, _ = await jobs.list_jobs(cid, status=JobStatus.succeeded)
        assert [j.id for j in succeeded] == [job.id]

        # unknown ids are "not found", not errors
        assert await jobs.get_job(cid, str(uuid.uuid4())) is None
        assert await jobs.get_job(cid, "not-a-uuid") is None

        deleted = await jobs.purge_client_jobs(cid)
        assert deleted == 2
        remaining, cursor = await jobs.list_jobs(cid)
        assert remaining == [] and cursor is None
    finally:
        try:
            await jobs.purge_client_jobs(cid)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        await close_pool()
