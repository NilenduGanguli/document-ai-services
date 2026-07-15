"""Unit tests for the static production-posture guard (``di/posture.py``).

Pure-logic: every branch of ``evaluate_static_posture`` is exercised with synthetic Settings, no
DB or network involved. The DB-observed counterpart (``di.db.evaluate_rls_posture``) is tested in
``tests/test_db_rls_posture.py``.
"""
from __future__ import annotations

from di.config import Settings
from di.posture import assert_production_posture, evaluate_static_posture


def _settings(**overrides) -> Settings:
    base = {"di_env": "prod", "rls_enabled": True, "migrations_mode": "verify",
            "pg_migration_user": "di_owner", "pg_user": "di_app"}
    base.update(overrides)
    return Settings(**base)


def test_clean_prod_posture_has_no_violations() -> None:
    assert evaluate_static_posture(_settings()) == []


def test_rls_disabled_is_a_violation() -> None:
    violations = evaluate_static_posture(_settings(rls_enabled=False))
    assert any("RLS_ENABLED" in v for v in violations)


def test_auto_mode_without_distinct_migration_user_is_a_violation() -> None:
    violations = evaluate_static_posture(
        _settings(migrations_mode="auto", pg_migration_user="")
    )
    assert any("MIGRATIONS_MODE=auto" in v for v in violations)


def test_auto_mode_with_migration_user_equal_to_pg_user_is_a_violation() -> None:
    violations = evaluate_static_posture(
        _settings(migrations_mode="auto", pg_migration_user="di_app", pg_user="di_app")
    )
    assert any("MIGRATIONS_MODE=auto" in v for v in violations)


def test_auto_mode_with_distinct_migration_user_is_clean() -> None:
    violations = evaluate_static_posture(
        _settings(migrations_mode="auto", pg_migration_user="di_owner", pg_user="di_app")
    )
    assert violations == []


def test_verify_mode_never_needs_a_migration_user() -> None:
    violations = evaluate_static_posture(
        _settings(migrations_mode="verify", pg_migration_user="")
    )
    assert violations == []


def test_multiple_violations_all_reported() -> None:
    violations = evaluate_static_posture(
        _settings(rls_enabled=False, migrations_mode="auto", pg_migration_user="")
    )
    assert len(violations) == 2


def test_evaluate_is_unconditional_even_outside_production() -> None:
    """evaluate_static_posture itself does not gate on is_production — callers do."""
    violations = evaluate_static_posture(_settings(di_env="local", rls_enabled=False))
    assert any("RLS_ENABLED" in v for v in violations)


def test_assert_is_a_noop_outside_production() -> None:
    assert_production_posture(_settings(di_env="local", rls_enabled=False))  # must not raise


def test_assert_raises_with_all_violations_named_in_production() -> None:
    settings = _settings(rls_enabled=False, migrations_mode="auto", pg_migration_user="")
    try:
        assert_production_posture(settings)
    except RuntimeError as exc:
        assert "RLS_ENABLED" in str(exc)
        assert "MIGRATIONS_MODE=auto" in str(exc)
        assert "prod" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_assert_is_clean_for_sound_production_settings() -> None:
    assert_production_posture(_settings())  # must not raise
