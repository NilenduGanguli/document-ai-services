"""Unit tests for the DB-observed RLS posture decision function (``di.db.evaluate_rls_posture``).

Pure-logic: every branch is exercised with synthetic facts (no live Postgres). The async gathering
wrapper (``di.db.assert_rls_posture``) that actually queries ``pg_roles``/``pg_policies`` is
covered by the integration suite (``tests/test_rls_isolation.py``, ``DI_RUN_INTEGRATION=1``).
"""
from __future__ import annotations

from di.db import _TENANT_TABLES, evaluate_rls_posture


def _clean_policies() -> dict[str, list[str]]:
    policies = {t: ["tenant_isolation"] for t in _TENANT_TABLES}
    policies["di_job"] = ["tenant_isolation", "worker_claim"]
    return policies


def _clean_predicates() -> dict[str, bool]:
    return dict.fromkeys(_TENANT_TABLES, True)


def _sound_kwargs(**overrides) -> dict:
    base = dict(
        rls_enabled=True, rolsuper=False, rolbypassrls=False, tenant_tables=_TENANT_TABLES,
        policies_by_table=_clean_policies(), tenant_isolation_predicate_ok=_clean_predicates(),
        partition_leaks=[],
    )
    base.update(overrides)
    return base


def test_sound_posture_has_no_violations() -> None:
    assert evaluate_rls_posture(**_sound_kwargs()) == []


def test_rls_disabled_is_a_violation() -> None:
    violations = evaluate_rls_posture(**_sound_kwargs(rls_enabled=False))
    assert any("RLS_ENABLED" in v for v in violations)


def test_superuser_connection_is_a_violation() -> None:
    violations = evaluate_rls_posture(**_sound_kwargs(rolsuper=True))
    assert any("superuser" in v for v in violations)


def test_bypassrls_connection_is_a_violation() -> None:
    violations = evaluate_rls_posture(**_sound_kwargs(rolbypassrls=True))
    assert any("BYPASSRLS" in v for v in violations)


def test_missing_tenant_isolation_policy_is_a_violation() -> None:
    policies = _clean_policies()
    del policies["knode"]
    violations = evaluate_rls_posture(**_sound_kwargs(policies_by_table=policies))
    assert any("knode" in v and "missing" in v for v in violations)


def test_wrong_predicate_is_a_violation() -> None:
    predicates = _clean_predicates()
    predicates["arep"] = False
    violations = evaluate_rls_posture(**_sound_kwargs(tenant_isolation_predicate_ok=predicates))
    assert any("arep" in v and "predicate" in v for v in violations)


def test_unexpected_extra_policy_is_a_violation() -> None:
    policies = _clean_policies()
    policies["di_documents"] = ["tenant_isolation", "some_other_policy"]
    violations = evaluate_rls_posture(**_sound_kwargs(policies_by_table=policies))
    assert any("di_documents" in v and "some_other_policy" in v for v in violations)


def test_worker_claim_on_di_job_is_allowed_not_flagged() -> None:
    """The reserved worker_claim policy on di_job must NOT trip the guard."""
    violations = evaluate_rls_posture(**_sound_kwargs())
    assert violations == []


def test_worker_claim_on_a_different_table_is_still_a_violation() -> None:
    """The exception is table-specific: worker_claim only belongs on di_job."""
    policies = _clean_policies()
    policies["di_blob"] = ["tenant_isolation", "worker_claim"]
    violations = evaluate_rls_posture(**_sound_kwargs(policies_by_table=policies))
    assert any("di_blob" in v and "worker_claim" in v for v in violations)


def test_partition_leak_is_a_violation() -> None:
    violations = evaluate_rls_posture(**_sound_kwargs(partition_leaks=["di.knode_p3"]))
    assert any("di.knode_p3" in v for v in violations)


def test_multiple_violations_all_reported() -> None:
    policies = _clean_policies()
    del policies["arep"]
    violations = evaluate_rls_posture(
        **_sound_kwargs(rolsuper=True, policies_by_table=policies,
                        partition_leaks=["di.arep_p0"])
    )
    assert len(violations) == 3


def test_tenant_tables_constant_has_exactly_ten_tables() -> None:
    """004_rls.sql's 7 + 005_hardening.sql's 3 == the 10 tables every posture check covers."""
    assert len(_TENANT_TABLES) == 10
    assert len(set(_TENANT_TABLES)) == 10
