"""Per-process rate limiting: a token-bucket backstop per API key, plus a short negative cache for
unresolvable keys so a credential-stuffing flood cannot turn into one Postgres lookup per request.

Both mechanisms are deliberately **per-process**, not distributed: with N replicas, the effective
fleet-wide limit is N times the configured value. This is a backstop against a leaked or
misbehaving key, not a precision control — exact global rate limits are the bank's API gateway's
job (documented in docs/specs/2026-07-15-enterprise-scale-plan.md §4's ADR). If a deployment runs
many uvicorn workers per pod, the multiplier is per-process (per worker), not per-pod — worth
calling out in the ops runbook if that topology is used.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from di.config import get_settings


@dataclass
class TokenBucket:
    """Classic token bucket. Pure and unit-testable: the clock is injected, not read from
    ``time.monotonic()`` directly, so refill math can be tested without real sleeps."""

    rps: float
    burst: int
    _tokens: float = field(init=False)
    _last: float = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.burst)
        self._last = 0.0

    def allow(self, *, now: float, cost: float = 1.0) -> bool:
        """Return True (and consume ``cost`` tokens) if the bucket has capacity at time ``now``."""
        elapsed = max(0.0, now - self._last)
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rps)
        self._last = now
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    def retry_after(self, *, cost: float = 1.0) -> float:
        """Seconds until ``cost`` tokens will be available, given the last-known fill level."""
        deficit = cost - self._tokens
        if deficit <= 0:
            return 0.0
        return deficit / self.rps if self.rps > 0 else float("inf")


# key_id -> bucket, and key_id -> last-touched monotonic time (for lazy pruning).
_buckets: dict[str, TokenBucket] = {}
_touched: dict[str, float] = {}
_PRUNE_IDLE_SECONDS = 600.0

# sha256(raw_key) -> expiry (monotonic). Only unresolvable keys are cached here — a successful
# resolution is cached by di.auth itself with its own (longer, positive-result) TTL.
_failed_auth_cache: dict[str, float] = {}


def _prune(now: float) -> None:
    stale = [k for k, t in _touched.items() if now - t > _PRUNE_IDLE_SECONDS]
    for k in stale:
        _buckets.pop(k, None)
        _touched.pop(k, None)


def check_rate_limit(key_id: str, *, rps: float | None = None, now: float | None = None,
                     ) -> tuple[bool, float]:
    """Check and consume one token for ``key_id``.

    Args:
        key_id: The principal's key id (bucket identity).
        rps: Refill rate; defaults to ``settings.rate_limit_default_rps``.
        now: Injected clock for tests; defaults to ``time.monotonic()``.

    Returns:
        ``(allowed, retry_after_seconds)``.
    """
    settings = get_settings()
    now = time.monotonic() if now is None else now
    effective_rps = rps if rps is not None else settings.rate_limit_default_rps
    bucket = _buckets.get(key_id)
    if bucket is None or bucket.rps != effective_rps:
        bucket = TokenBucket(rps=effective_rps, burst=settings.rate_limit_burst)
        _buckets[key_id] = bucket
    _touched[key_id] = now
    if len(_touched) > 10_000:  # pragma: no cover - defensive; keeps memory bounded under churn
        _prune(now)
    if bucket.allow(now=now):
        return True, 0.0
    return False, bucket.retry_after()


def check_failed_auth_backstop(key_hash: str, *, now: float | None = None) -> bool:
    """Return True if ``key_hash`` was recently confirmed invalid — caller should 401 immediately
    without a DB round-trip. Call :func:`record_auth_failure` after a real lookup misses."""
    now = time.monotonic() if now is None else now
    expiry = _failed_auth_cache.get(key_hash)
    return expiry is not None and now < expiry


def record_auth_failure(key_hash: str, *, now: float | None = None) -> None:
    settings = get_settings()
    now = time.monotonic() if now is None else now
    _failed_auth_cache[key_hash] = now + settings.auth_failure_cache_seconds
    if len(_failed_auth_cache) > 10_000:  # pragma: no cover - defensive bound
        expired = [k for k, exp in _failed_auth_cache.items() if now >= exp]
        for k in expired:
            _failed_auth_cache.pop(k, None)


def reset() -> None:
    """Clear all in-memory state. Test hook."""
    _buckets.clear()
    _touched.clear()
    _failed_auth_cache.clear()
