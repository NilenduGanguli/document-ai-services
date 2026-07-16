"""Observability — Prometheus metrics and a process-local readiness registry.

Two concerns live here. Both are import-safe (no I/O at import) and never fatal.

**Readiness.** A registry of component health (``db``, ``migrations``, ``pgvector``,
``retrieval``, ``ocr``, ``blob``). Startup records what actually worked, so ``/health`` can
report *degraded* instead of a green 200 over a boot where migrations failed, embeddings fell
back to a stub, or pgvector was absent. Only :data:`REQUIRED_COMPONENTS` gate
:meth:`Readiness.ready`; the rest are informational — a missing pgvector degrades search but the
service still serves. A required component that was never set counts as **not ready**: unknown is
never assumed healthy, so a boot path that dies before reporting cannot fail open.

**Metrics.** Thin wrappers over ``prometheus_client``, which is an optional dependency. If it is
not importable every ``observe_*`` becomes a silent no-op and :func:`metrics_enabled` returns
``False`` — instrumentation must never be the reason the service fails to boot.

The compliance-critical signals are ``di_gate_decisions_total`` (what the gate decided, split by
sensitivity bucket) and ``di_llm_egress_total`` (what share of documents actually left the trust
boundary). Both are cheap to scrape and are the numbers an auditor asks for.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

try:  # prometheus_client is optional — the app must boot and run without it.
    import prometheus_client as _prom
except ImportError:  # pragma: no cover - depends on the installed dependency set
    _prom = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
#: Components that must be healthy for the service to be considered ready.
#: Grown per phase of the enterprise-scale-out plan: db/migrations (baseline) + posture/rls
#: (RLS production posture) — see docs/specs/2026-07-15-enterprise-scale-plan.md interaction #16.
REQUIRED_COMPONENTS: tuple[str, ...] = ("db", "migrations", "posture", "rls")

#: The component vocabulary. Anything outside this set is still accepted by
#: :meth:`Readiness.set`; this tuple documents what the boot path is expected to report.
KNOWN_COMPONENTS: tuple[str, ...] = (
    "db",
    "migrations",
    "posture",
    "rls",
    "pgvector",
    "retrieval",
    "ocr",
    "blob",
    "auth",
    "audit",
    "queue",
)


class ComponentState(BaseModel):
    """Health of a single component as last reported.

    Attributes:
        ok: Whether the component is healthy.
        detail: Human-readable reason, most useful when ``ok`` is ``False``.
        extra: Structured context (e.g. ``{"dim": 768, "stub": True}``).
    """

    ok: bool
    detail: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class Readiness:
    """A mutable registry of component health.

    Guarded by a lock so a threadpool-offloaded startup step and the event loop can report
    concurrently. No I/O happens here: callers probe their own dependency and report the result.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, ComponentState] = {}

    def set(self, component: str, ok: bool, detail: str = "", **extra: Any) -> None:
        """Record (or replace) the state of ``component``.

        Args:
            component: Component name — see :data:`KNOWN_COMPONENTS`.
            ok: Whether the component is healthy.
            detail: Human-readable reason, most useful when ``ok`` is ``False``.
            **extra: Structured context stored on :attr:`ComponentState.extra`.
        """
        with self._lock:
            self._state[component] = ComponentState(ok=ok, detail=detail, extra=dict(extra))

    def get(self, component: str) -> ComponentState | None:
        """Return the state of ``component``, or ``None`` if it was never reported."""
        with self._lock:
            state = self._state.get(component)
        return state.model_copy(deep=True) if state is not None else None

    def snapshot(self) -> dict[str, ComponentState]:
        """Return a deep copy of every reported component state.

        The copy means callers (e.g. a ``/health`` handler) cannot mutate the registry.
        """
        with self._lock:
            return {name: state.model_copy(deep=True) for name, state in self._state.items()}

    def ready(self) -> bool:
        """Return ``True`` only if every component in :data:`REQUIRED_COMPONENTS` is healthy.

        A required component that was never reported makes this ``False`` — unknown is not ready.
        """
        snap = self.snapshot()
        return all(
            (state := snap.get(name)) is not None and state.ok for name in REQUIRED_COMPONENTS
        )

    def degraded(self) -> list[str]:
        """Return the sorted names of components that are not healthy.

        Includes required components that were never reported, so this always explains a
        :meth:`ready` of ``False``.
        """
        snap = self.snapshot()
        names = {name for name, state in snap.items() if not state.ok}
        names.update(name for name in REQUIRED_COMPONENTS if name not in snap)
        return sorted(names)


#: Process-wide readiness registry. Written by the boot path, read by ``/health``.
READINESS = Readiness()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
#: Registry every collector here is bound to. Defaults to the ``prometheus_client`` global so
#: process/GC collectors are exposed alongside ours; swap it before import-time construction to
#: isolate (e.g. in a multiprocess setup).
_REGISTRY: Any = _prom.REGISTRY if _prom is not None else None

# Ingest stages span sub-second gating to multi-minute OCR, so the default 10s-max buckets would
# saturate. Search is an interactive path and gets a tighter, low-latency spread.
_STAGE_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, float("inf"))
_SEARCH_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, float("inf"))

_DISABLED_CONTENT_TYPE = "text/plain; charset=utf-8"
_DISABLED_PAYLOAD = b"# metrics disabled: prometheus_client is not installed\n"


@dataclass(frozen=True)
class _Metrics:
    """The collector set, built once at import. ``Any`` because the dep may be absent."""

    ingest_total: Any
    stage_seconds: Any
    stage_failures: Any
    gate_decisions: Any
    llm_egress: Any
    ocr_engine: Any
    search_seconds: Any
    jobs_inflight: Any
    queue_depth: Any
    queue_oldest_age: Any
    jobs_claimed: Any
    jobs_retried: Any
    jobs_dead: Any
    job_duration: Any
    worker_leases_lost: Any


def _find_collector(name: str) -> Any | None:
    """Return an already-registered collector for ``name``, if any.

    ``prometheus_client`` strips the ``_total`` suffix from counter names internally, so both
    spellings are probed.
    """
    mapping = getattr(_REGISTRY, "_names_to_collectors", {})
    for key in (name, name.removesuffix("_total")):
        collector = mapping.get(key)
        if collector is not None:
            return collector
    return None


def _get_or_create(factory: Any, name: str, documentation: str, **kwargs: Any) -> Any:
    """Register a collector, reusing an identically-named one already in the registry.

    The registry is a module-global that outlives this module's own globals, so a re-import
    (pytest collecting several test modules) would otherwise trip the duplicate-timeseries guard.
    """
    try:
        return factory(name, documentation, registry=_REGISTRY, **kwargs)
    except ValueError:
        existing = _find_collector(name)
        if existing is None:
            raise
        return existing


def _build_metrics() -> _Metrics | None:
    """Construct every collector, or return ``None`` if metrics cannot be set up."""
    if _prom is None:
        return None
    try:
        return _Metrics(
            ingest_total=_get_or_create(
                _prom.Counter,
                "di_ingest_total",
                "Document ingest runs by outcome (started|succeeded|failed|noop).",
                labelnames=("outcome",),
            ),
            stage_seconds=_get_or_create(
                _prom.Histogram,
                "di_ingest_stage_seconds",
                "Wall-clock duration of each ingest pipeline stage.",
                labelnames=("stage", "ok"),
                buckets=_STAGE_BUCKETS,
            ),
            stage_failures=_get_or_create(
                _prom.Counter,
                "di_stage_failures_total",
                "Ingest pipeline stage failures.",
                labelnames=("stage",),
            ),
            gate_decisions=_get_or_create(
                _prom.Counter,
                "di_gate_decisions_total",
                "Gate decisions by decision and sensitivity bucket (compliance signal).",
                labelnames=("decision", "sensitivity"),
            ),
            llm_egress=_get_or_create(
                _prom.Counter,
                "di_llm_egress_total",
                "Documents by whether content was allowed out to the LLM (compliance signal).",
                labelnames=("allowed",),
            ),
            ocr_engine=_get_or_create(
                _prom.Counter,
                "di_ocr_engine_total",
                "OCR runs by engine.",
                labelnames=("engine",),
            ),
            search_seconds=_get_or_create(
                _prom.Histogram,
                "di_search_seconds",
                "Search request latency.",
                buckets=_SEARCH_BUCKETS,
            ),
            jobs_inflight=_get_or_create(
                _prom.Gauge,
                "di_jobs_inflight",
                "Ingest jobs currently in flight.",
            ),
            queue_depth=_get_or_create(
                _prom.Gauge,
                "di_queue_depth",
                "di_job rows by kind and status (refreshed from queue_stats()).",
                labelnames=("kind", "status"),
            ),
            queue_oldest_age=_get_or_create(
                _prom.Gauge,
                "di_queue_oldest_age_seconds",
                "Age of the oldest queued job, by kind.",
                labelnames=("kind",),
            ),
            jobs_claimed=_get_or_create(
                _prom.Counter,
                "di_jobs_claimed_total",
                "Jobs claimed by a worker, by kind.",
                labelnames=("kind",),
            ),
            jobs_retried=_get_or_create(
                _prom.Counter,
                "di_jobs_retried_total",
                "Jobs requeued after a retryable failure or lease expiry, by kind.",
                labelnames=("kind",),
            ),
            jobs_dead=_get_or_create(
                _prom.Counter,
                "di_jobs_dead_total",
                "Jobs dead-lettered (attempts exhausted) — page-worthy at any nonzero rate.",
                labelnames=("kind",),
            ),
            job_duration=_get_or_create(
                _prom.Histogram,
                "di_job_duration_seconds",
                "Wall-clock time from first claim to terminal status, by kind.",
                labelnames=("kind",),
                buckets=_STAGE_BUCKETS,
            ),
            worker_leases_lost=_get_or_create(
                _prom.Counter,
                "di_worker_leases_lost_total",
                "Times a worker's heartbeat found its own job already reclaimed elsewhere.",
            ),
        )
    except Exception:  # noqa: BLE001 - metrics are never worth failing boot over
        logger.warning("could not build prometheus collectors; metrics disabled", exc_info=True)
        return None


_METRICS: _Metrics | None = _build_metrics()


def _bool_label(value: bool) -> str:
    """Render a bool as a stable Prometheus label value."""
    return "true" if value else "false"


def metrics_enabled() -> bool:
    """Return whether metrics are being collected."""
    return _METRICS is not None


def metrics_response() -> tuple[bytes, str]:
    """Render the current metrics for the ``/metrics`` route.

    Returns:
        ``(payload, content_type)``. When metrics are disabled the payload is an explanatory
        comment rather than an error, so a scraper gets a valid, empty exposition.
    """
    if _prom is None or _METRICS is None:
        return _DISABLED_PAYLOAD, _DISABLED_CONTENT_TYPE
    return _prom.generate_latest(_REGISTRY), _prom.CONTENT_TYPE_LATEST


def observe_ingest(outcome: str) -> None:
    """Count an ingest run.

    Args:
        outcome: One of ``started``, ``succeeded``, ``failed``, ``noop``.
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.ingest_total.labels(outcome=str(outcome)).inc()


def observe_stage(stage: str, seconds: float, ok: bool = True) -> None:
    """Record an ingest stage's duration, and a failure when it did not succeed.

    Args:
        stage: Stage name (``ocr``, ``gate``, ``extract``, ``subtree``, ``arep``, ``merge``).
        seconds: Wall-clock duration of the stage.
        ok: Whether the stage succeeded. ``False`` also increments ``di_stage_failures_total``.
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.stage_seconds.labels(stage=str(stage), ok=_bool_label(ok)).observe(seconds)
    if not ok:
        metrics.stage_failures.labels(stage=str(stage)).inc()


def observe_gate(decision: str, sensitivity: str) -> None:
    """Count a gate decision — the compliance-critical signal.

    Args:
        decision: A :class:`~di.models.GateDecision` value.
        sensitivity: A :class:`~di.models.SensitivityBucket` value.
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.gate_decisions.labels(decision=str(decision), sensitivity=str(sensitivity)).inc()


def observe_llm_egress(allowed: bool) -> None:
    """Count whether a document's content was allowed out to the LLM.

    Args:
        allowed: ``True`` when content crossed the trust boundary.
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.llm_egress.labels(allowed=_bool_label(allowed)).inc()


def observe_ocr(engine: str) -> None:
    """Count an OCR run.

    Args:
        engine: Engine that produced the text (``azure``, ``tesseract``, ``pypdf``, ``text``).
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.ocr_engine.labels(engine=str(engine)).inc()


def observe_search(seconds: float) -> None:
    """Record a search request's latency.

    Args:
        seconds: Wall-clock duration of the request.
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.search_seconds.observe(seconds)


def set_jobs_inflight(n: int) -> None:
    """Set the number of ingest jobs currently in flight.

    Args:
        n: Current in-flight job count.
    """
    metrics = _METRICS
    if metrics is None:
        return
    metrics.jobs_inflight.set(n)


def set_queue_stats(stats: list[dict[str, Any]]) -> None:
    """Refresh the queue depth/age gauges from :func:`di.jobs.queue_stats`'s rows.

    Called by every worker's reaper cycle (and by the API process too, so the depth metric does
    not go stale exactly when every worker is down — the incident it exists to catch).
    """
    metrics = _METRICS
    if metrics is None:
        return
    oldest_by_kind: dict[str, float] = {}
    for row in stats:
        kind, status = str(row["kind"]), str(row["status"])
        metrics.queue_depth.labels(kind=kind, status=status).set(row["n"])
        if status == "queued":
            age = row.get("oldest_age_seconds") or 0.0
            oldest_by_kind[kind] = max(oldest_by_kind.get(kind, 0.0), float(age))
    for kind, age in oldest_by_kind.items():
        metrics.queue_oldest_age.labels(kind=kind).set(age)


def observe_job_claimed(kind: str) -> None:
    metrics = _METRICS
    if metrics is None:
        return
    metrics.jobs_claimed.labels(kind=str(kind)).inc()


def observe_job_retried(kind: str, n: int = 1) -> None:
    metrics = _METRICS
    if metrics is None or n <= 0:
        return
    metrics.jobs_retried.labels(kind=str(kind)).inc(n)


def observe_job_dead(kind: str, n: int = 1) -> None:
    metrics = _METRICS
    if metrics is None or n <= 0:
        return
    metrics.jobs_dead.labels(kind=str(kind)).inc(n)


def observe_job_duration(kind: str, seconds: float) -> None:
    metrics = _METRICS
    if metrics is None:
        return
    metrics.job_duration.labels(kind=str(kind)).observe(seconds)


def observe_lease_lost() -> None:
    metrics = _METRICS
    if metrics is None:
        return
    metrics.worker_leases_lost.inc()


@contextmanager
def stage_timer(stage: str) -> Iterator[None]:
    """Time a pipeline stage and report it via :func:`observe_stage`.

    An exception marks the stage failed and is re-raised unchanged; ``BaseException`` is included
    so a cancelled ingest is not recorded as a success.

    Args:
        stage: Stage name passed through to :func:`observe_stage`.

    Yields:
        ``None`` — the block runs inside the timing window.
    """
    start = time.perf_counter()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        observe_stage(stage, time.perf_counter() - start, ok=ok)
