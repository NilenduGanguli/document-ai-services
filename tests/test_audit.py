"""Unit tests for di/audit.py's pure logic: tenant resolution and record shaping.

The AccessLogWriter's queue/flush behavior needs a live DB and is exercised by the smoke test and
manually via the live stack; this file covers what's testable without one.
"""
from __future__ import annotations

import json

import pytest

from di.audit import AccessRecord, AuditUnavailable, resolve_audit_client_id


# ---------------------------------------------------------------------------
# resolve_audit_client_id — the tenant-resolution precedence
# ---------------------------------------------------------------------------
def test_path_param_wins_first():
    assert resolve_audit_client_id({"client_id": "acme"}, {"client_id": "other"}, "third") == "acme"


def test_falls_back_to_query_param():
    assert resolve_audit_client_id({}, {"client_id": "acme"}, "third") == "acme"


def test_falls_back_to_state_client_id():
    """Covers handlers whose client_id arrives in the multipart body (ingest)."""
    assert resolve_audit_client_id({}, {}, "acme") == "acme"


def test_none_when_nothing_resolves():
    assert resolve_audit_client_id({}, {}, None) is None


def test_query_params_none_is_handled():
    """The jobs/nodes routes pass real query params; some call sites may have none at all."""
    assert resolve_audit_client_id({}, None, "acme") == "acme"


def test_empty_path_param_falls_through():
    """An empty-string path param (should not happen in practice) must not shadow a real query
    param or state value."""
    assert resolve_audit_client_id({"client_id": ""}, {"client_id": "acme"}, None) == "acme"


# ---------------------------------------------------------------------------
# AccessRecord
# ---------------------------------------------------------------------------
def test_as_log_line_is_valid_json_with_expected_fields():
    r = AccessRecord(method="GET", route="/api/v1/clients/{client_id}/facts", status=200,
                     key_id="k1", principal="acme-key", client_id="acme", masked=True,
                     request_id="req-1")
    parsed = json.loads(r.as_log_line())
    assert parsed["method"] == "GET"
    assert parsed["route"] == "/api/v1/clients/{client_id}/facts"
    assert parsed["status"] == 200
    assert parsed["client_id"] == "acme"
    assert parsed["masked"] is True


def test_as_log_line_never_raises_on_extra_content():
    r = AccessRecord(method="POST", route="/api/v1/ingest", status=202,
                     extra={"nested": {"a": 1}, "note": "ok"})
    parsed = json.loads(r.as_log_line())
    assert parsed["nested"] == {"a": 1}


def test_defaults_are_none_when_unresolved():
    r = AccessRecord(method="GET", route="/health", status=200)
    assert r.client_id is None
    assert r.key_id is None
    assert r.masked is None


# ---------------------------------------------------------------------------
# AuditUnavailable
# ---------------------------------------------------------------------------
def test_audit_unavailable_is_an_exception():
    with pytest.raises(AuditUnavailable):
        raise AuditUnavailable("queue saturated")
