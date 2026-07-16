"""Unit tests for di/ratelimit.py — pure logic, injected clock, no real sleeps."""
from __future__ import annotations

import pytest

from di import ratelimit
from di.ratelimit import (
    TokenBucket,
    check_failed_auth_backstop,
    check_rate_limit,
    record_auth_failure,
)


@pytest.fixture(autouse=True)
def _reset():
    ratelimit.reset()
    yield
    ratelimit.reset()


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------
def test_bucket_starts_full():
    b = TokenBucket(rps=10, burst=5)
    for _ in range(5):
        assert b.allow(now=0.0) is True
    assert b.allow(now=0.0) is False


def test_bucket_refills_over_time():
    b = TokenBucket(rps=10, burst=5)
    for _ in range(5):
        b.allow(now=0.0)
    assert b.allow(now=0.0) is False
    assert b.allow(now=0.1) is True  # 0.1s * 10rps = 1 token refilled


def test_bucket_never_exceeds_burst():
    b = TokenBucket(rps=1000, burst=3)
    b.allow(now=0.0)
    assert b.allow(now=1000.0) is True  # huge elapsed time, still capped at burst
    assert b.allow(now=1000.0) is True
    assert b.allow(now=1000.0) is True
    assert b.allow(now=1000.0) is False  # only 3 tokens max


def test_retry_after_zero_when_tokens_available():
    b = TokenBucket(rps=10, burst=5)
    assert b.retry_after() == 0.0


def test_retry_after_positive_when_exhausted():
    b = TokenBucket(rps=10, burst=1)
    b.allow(now=0.0)
    assert b.retry_after() > 0.0


def test_cost_can_exceed_one():
    b = TokenBucket(rps=10, burst=5)
    assert b.allow(now=0.0, cost=3) is True
    assert b.allow(now=0.0, cost=3) is False  # only 2 left


# ---------------------------------------------------------------------------
# check_rate_limit
# ---------------------------------------------------------------------------
def test_check_rate_limit_allows_within_burst():
    for _ in range(5):
        allowed, retry = check_rate_limit("key-a", rps=10, now=0.0)
        assert allowed is True
        assert retry == 0.0


def test_check_rate_limit_denies_beyond_burst_and_reports_retry_after():
    for _ in range(100):
        check_rate_limit("key-b", rps=10, now=0.0)
    allowed, retry = check_rate_limit("key-b", rps=10, now=0.0)
    assert allowed is False
    assert retry > 0.0


def test_check_rate_limit_buckets_are_per_key():
    for _ in range(100):
        check_rate_limit("key-c", rps=10, now=0.0)
    allowed, _ = check_rate_limit("key-d", rps=10, now=0.0)
    assert allowed is True  # a different key has its own bucket


def test_rps_change_resets_the_bucket():
    """If a key's configured rate changes (e.g. admin edits rate_limit_rps), the stale bucket
    (sized for the old rate) must not silently persist."""
    for _ in range(5):
        check_rate_limit("key-e", rps=5, now=0.0)
    allowed, _ = check_rate_limit("key-e", rps=1000, now=0.0)
    assert allowed is True  # new bucket sized for the new rate, not exhausted


# ---------------------------------------------------------------------------
# failed-auth backstop
# ---------------------------------------------------------------------------
def test_failed_auth_not_cached_initially():
    assert check_failed_auth_backstop("somehash", now=0.0) is False


def test_failed_auth_cached_after_record():
    record_auth_failure("somehash", now=0.0)
    assert check_failed_auth_backstop("somehash", now=0.1) is True


def test_failed_auth_cache_expires():
    record_auth_failure("somehash", now=0.0)
    # auth_failure_cache_seconds defaults to 5.0 in Settings
    assert check_failed_auth_backstop("somehash", now=100.0) is False


def test_failed_auth_cache_is_per_hash():
    record_auth_failure("hash-1", now=0.0)
    assert check_failed_auth_backstop("hash-2", now=0.0) is False
