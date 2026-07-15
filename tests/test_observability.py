"""Observability tests — readiness semantics, stage timing, and metric-free degradation.

Pure-logic (no DB / no network). The metrics assertions run against whichever dependency set is
installed: the collector tests skip without ``prometheus_client``, while the no-op tests force the
disabled path so it is covered even where the optional dep *is* present.
"""
from __future__ import annotations

import pytest

from di import observability as obs


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
def test_required_components_and_singleton():
    assert obs.REQUIRED_COMPONENTS == ("db", "migrations")
    assert isinstance(obs.READINESS, obs.Readiness)
    for name in obs.REQUIRED_COMPONENTS:
        assert name in obs.KNOWN_COMPONENTS


def test_fresh_registry_is_not_ready():
    """Unknown required component => not ready. A boot that never reported cannot fail open."""
    r = obs.Readiness()
    assert r.ready() is False
    assert r.snapshot() == {}
    assert r.degraded() == ["db", "migrations"]


def test_partially_reported_required_component_is_not_ready():
    r = obs.Readiness()
    r.set("db", True)
    assert r.ready() is False
    assert r.degraded() == ["migrations"]


def test_all_required_ok_is_ready():
    r = obs.Readiness()
    r.set("db", True)
    r.set("migrations", True)
    assert r.ready() is True
    assert r.degraded() == []


def test_failed_required_component_is_not_ready():
    r = obs.Readiness()
    r.set("db", True)
    r.set("migrations", False, "relation knode does not exist")
    assert r.ready() is False
    assert r.degraded() == ["migrations"]


def test_informational_component_does_not_gate_readiness():
    """No pgvector degrades search but must not take the service out of rotation."""
    r = obs.Readiness()
    r.set("db", True)
    r.set("migrations", True)
    r.set("pgvector", False, "extension not installed")
    r.set("retrieval", False, "stub embeddings")
    assert r.ready() is True
    assert r.degraded() == ["pgvector", "retrieval"]


def test_get_returns_state_and_none_for_unknown():
    r = obs.Readiness()
    r.set("ocr", False, "azure key missing", engine="tesseract", pages=3)
    state = r.get("ocr")
    assert state is not None
    assert state.ok is False
    assert state.detail == "azure key missing"
    assert state.extra == {"engine": "tesseract", "pages": 3}
    assert r.get("nope") is None


def test_set_replaces_previous_state():
    r = obs.Readiness()
    r.set("db", False, "connection refused")
    r.set("db", True, "", pool_max=16)
    state = r.get("db")
    assert state is not None
    assert state.ok is True
    assert state.detail == ""
    assert state.extra == {"pool_max": 16}


def test_default_component_state_fields():
    state = obs.ComponentState(ok=True)
    assert state.detail == ""
    assert state.extra == {}


def test_snapshot_is_a_copy_and_cannot_mutate_the_registry():
    r = obs.Readiness()
    r.set("db", True, "up", host="localhost")
    snap = r.snapshot()
    snap["db"].ok = False
    snap["db"].extra["host"] = "tampered"
    snap["injected"] = obs.ComponentState(ok=False)

    live = r.get("db")
    assert live is not None
    assert live.ok is True
    assert live.extra == {"host": "localhost"}
    assert "injected" not in r.snapshot()
    assert r.ready() is False  # migrations still unknown — unaffected by the tampering


def test_readiness_instances_are_independent():
    a, b = obs.Readiness(), obs.Readiness()
    a.set("db", True)
    assert b.get("db") is None


# ---------------------------------------------------------------------------
# stage_timer
# ---------------------------------------------------------------------------
@pytest.fixture
def recorded(monkeypatch) -> list[tuple[str, float, bool]]:
    """Capture observe_stage calls made by stage_timer."""
    calls: list[tuple[str, float, bool]] = []

    def _fake(stage: str, seconds: float, ok: bool = True) -> None:
        calls.append((stage, seconds, ok))

    monkeypatch.setattr(obs, "observe_stage", _fake)
    return calls


def test_stage_timer_records_duration_on_success(recorded):
    with obs.stage_timer("ocr"):
        pass
    assert len(recorded) == 1
    stage, seconds, ok = recorded[0]
    assert stage == "ocr"
    assert ok is True
    assert seconds >= 0.0


def test_stage_timer_measures_elapsed_time(recorded, monkeypatch):
    """Duration is the delta between clock reads, not a constant — checked on a fake clock."""
    ticks = iter([10.0, 10.25])
    monkeypatch.setattr(obs.time, "perf_counter", lambda: next(ticks))
    with obs.stage_timer("extract"):
        pass
    assert recorded == [("extract", pytest.approx(0.25), True)]


def test_stage_timer_records_failure_and_reraises(recorded):
    with pytest.raises(RuntimeError, match="boom"):
        with obs.stage_timer("gate"):
            raise RuntimeError("boom")
    assert len(recorded) == 1
    stage, seconds, ok = recorded[0]
    assert stage == "gate"
    assert ok is False
    assert seconds >= 0.0


def test_stage_timer_records_failure_on_base_exception(recorded):
    """A cancelled stage is not a success."""
    with pytest.raises(KeyboardInterrupt):
        with obs.stage_timer("arep"):
            raise KeyboardInterrupt
    assert recorded[0][0] == "arep"
    assert recorded[0][2] is False


def test_stage_timer_does_not_swallow_the_original_exception(recorded):
    class Custom(Exception):
        pass

    err = Custom("original")
    with pytest.raises(Custom) as excinfo:
        with obs.stage_timer("merge"):
            raise err
    assert excinfo.value is err


# ---------------------------------------------------------------------------
# Degraded mode: prometheus_client unavailable
# ---------------------------------------------------------------------------
@pytest.fixture
def metrics_unavailable(monkeypatch):
    """Force the no-prometheus_client path regardless of what is installed."""
    monkeypatch.setattr(obs, "_prom", None)
    monkeypatch.setattr(obs, "_METRICS", None)


def test_metrics_disabled_reports_false(metrics_unavailable):
    assert obs.metrics_enabled() is False


def test_metrics_response_is_valid_when_disabled(metrics_unavailable):
    payload, content_type = obs.metrics_response()
    assert isinstance(payload, bytes)
    assert isinstance(content_type, str)
    assert content_type.startswith("text/plain")
    assert payload.startswith(b"#")  # a comment: a valid, empty exposition


def test_every_observer_is_a_silent_noop_when_disabled(metrics_unavailable):
    obs.observe_ingest("started")
    obs.observe_ingest("succeeded")
    obs.observe_ingest("failed")
    obs.observe_ingest("noop")
    obs.observe_stage("ocr", 1.5)
    obs.observe_stage("ocr", 1.5, ok=False)
    obs.observe_gate("SEND_TO_LLM", "LOW")
    obs.observe_llm_egress(True)
    obs.observe_llm_egress(False)
    obs.observe_ocr("azure")
    obs.observe_search(0.02)
    obs.set_jobs_inflight(3)


def test_stage_timer_works_when_metrics_disabled(metrics_unavailable):
    with obs.stage_timer("subtree"):
        pass
    with pytest.raises(ValueError, match="nope"):
        with obs.stage_timer("subtree"):
            raise ValueError("nope")


def test_build_metrics_returns_none_without_prometheus(monkeypatch):
    monkeypatch.setattr(obs, "_prom", None)
    assert obs._build_metrics() is None


def test_build_metrics_disables_rather_than_raises_on_registry_failure(monkeypatch):
    """A broken registry degrades to no metrics; it must never propagate out of import."""

    class _Boom:
        REGISTRY = None

        @staticmethod
        def Counter(*args, **kwargs):  # noqa: N802 - mirrors the prometheus_client API
            raise RuntimeError("registry exploded")

    monkeypatch.setattr(obs, "_prom", _Boom)
    assert obs._build_metrics() is None


# ---------------------------------------------------------------------------
# Live collectors (skipped when the optional dep is absent)
#
# Marked per-test, not module-wide: without prometheus_client everything above must still run —
# that is the environment the no-op path exists for.
# ---------------------------------------------------------------------------
requires_prometheus = pytest.mark.skipif(
    not obs.metrics_enabled(), reason="prometheus_client is an optional dependency"
)


def _sample(name: str, labels: dict[str, str] | None = None) -> float | None:
    return obs._REGISTRY.get_sample_value(name, labels or {})


@requires_prometheus
def test_metrics_enabled_with_prometheus_installed():
    assert obs.metrics_enabled() is True
    assert obs._METRICS is not None


@requires_prometheus
def test_metrics_response_exposes_the_declared_metric_names():
    import prometheus_client as prom

    obs.observe_ingest("succeeded")
    obs.observe_gate("SEND_TO_LLM", "LOW")
    obs.observe_llm_egress(True)
    obs.observe_ocr("azure")
    obs.observe_stage("ocr", 0.1)
    obs.observe_search(0.01)
    obs.set_jobs_inflight(0)

    payload, content_type = obs.metrics_response()
    assert content_type == prom.CONTENT_TYPE_LATEST
    text = payload.decode()
    for name in (
        "di_ingest_total",
        "di_ingest_stage_seconds",
        "di_gate_decisions_total",
        "di_llm_egress_total",
        "di_ocr_engine_total",
        "di_search_seconds",
        "di_jobs_inflight",
    ):
        assert name in text


@requires_prometheus
def test_observe_ingest_increments_by_outcome():
    before = _sample("di_ingest_total", {"outcome": "failed"}) or 0.0
    obs.observe_ingest("failed")
    assert _sample("di_ingest_total", {"outcome": "failed"}) == before + 1


@requires_prometheus
def test_observe_gate_labels_decision_and_sensitivity():
    labels = {"decision": "DETERMINISTIC_ONLY", "sensitivity": "CRITICAL"}
    before = _sample("di_gate_decisions_total", labels) or 0.0
    obs.observe_gate("DETERMINISTIC_ONLY", "CRITICAL")
    assert _sample("di_gate_decisions_total", labels) == before + 1


@requires_prometheus
def test_observe_gate_accepts_str_enums():
    from di.models import GateDecision, SensitivityBucket

    labels = {"decision": "REDACT_THEN_SEND", "sensitivity": "HIGH"}
    before = _sample("di_gate_decisions_total", labels) or 0.0
    obs.observe_gate(GateDecision.redact_then_send, SensitivityBucket.high)
    assert _sample("di_gate_decisions_total", labels) == before + 1


@requires_prometheus
def test_observe_llm_egress_uses_true_false_labels():
    before = _sample("di_llm_egress_total", {"allowed": "false"}) or 0.0
    obs.observe_llm_egress(False)
    assert _sample("di_llm_egress_total", {"allowed": "false"}) == before + 1


@requires_prometheus
def test_observe_stage_failure_also_counts_a_stage_failure():
    before = _sample("di_stage_failures_total", {"stage": "extract"}) or 0.0
    obs.observe_stage("extract", 2.0, ok=False)
    assert _sample("di_stage_failures_total", {"stage": "extract"}) == before + 1
    assert _sample("di_ingest_stage_seconds_count", {"stage": "extract", "ok": "false"}) >= 1


@requires_prometheus
def test_observe_stage_success_does_not_count_a_failure():
    before = _sample("di_stage_failures_total", {"stage": "gate"}) or 0.0
    obs.observe_stage("gate", 0.01)
    assert (_sample("di_stage_failures_total", {"stage": "gate"}) or 0.0) == before


@requires_prometheus
def test_observe_search_and_jobs_inflight():
    before = _sample("di_search_seconds_count") or 0.0
    obs.observe_search(0.05)
    assert _sample("di_search_seconds_count") == before + 1
    obs.set_jobs_inflight(7)
    assert _sample("di_jobs_inflight") == 7
    obs.set_jobs_inflight(0)
    assert _sample("di_jobs_inflight") == 0


@requires_prometheus
def test_rebuilding_metrics_reuses_collectors_instead_of_raising():
    """Module re-import (pytest) must not trip the duplicate-timeseries guard."""
    rebuilt = obs._build_metrics()
    assert rebuilt is not None
    assert rebuilt.ingest_total is obs._METRICS.ingest_total
    assert rebuilt.search_seconds is obs._METRICS.search_seconds
    assert rebuilt.jobs_inflight is obs._METRICS.jobs_inflight


def test_get_or_create_reraises_when_the_name_is_not_reusable(monkeypatch):
    """A ValueError that is not a duplicate registration must surface, not be swallowed."""

    def _factory(*args, **kwargs):
        raise ValueError("invalid metric name")

    with pytest.raises(ValueError, match="invalid metric name"):
        obs._get_or_create(_factory, "di_definitely_not_registered", "doc")
