"""Unit tests for the static production-posture guard (``di/posture.py``).

Pure-logic: every branch of ``evaluate_static_posture`` is exercised with synthetic Settings, no
DB or network involved. The DB-observed counterpart (``di.db.evaluate_rls_posture``) is tested in
``tests/test_db_rls_posture.py``.
"""
from __future__ import annotations

from di.config import Settings
from di.posture import assert_production_posture, evaluate_static_posture


def _settings(**overrides) -> Settings:
    base = {
        "di_env": "prod", "rls_enabled": True, "migrations_mode": "verify",
        "pg_migration_user": "di_owner", "pg_user": "di_app",
        "auth_enabled": True, "mask_by_default": True, "di_bootstrap_api_key": "",
        "access_audit_enabled": True, "access_audit_strict": True,
        "instance_fingerprint_hmac_key": "a-deployment-scoped-secret-from-secret-manager",
    }
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


def test_auth_disabled_is_a_violation() -> None:
    violations = evaluate_static_posture(_settings(auth_enabled=False))
    assert any("AUTH_ENABLED" in v for v in violations)


def test_mask_by_default_false_is_a_violation() -> None:
    violations = evaluate_static_posture(_settings(mask_by_default=False))
    assert any("MASK_BY_DEFAULT" in v for v in violations)


def test_demo_bootstrap_key_is_a_violation() -> None:
    violations = evaluate_static_posture(
        _settings(di_bootstrap_api_key="di_local_dev_key_change_me")
    )
    assert any("demo value" in v for v in violations)


def test_short_bootstrap_key_is_a_violation() -> None:
    violations = evaluate_static_posture(_settings(di_bootstrap_api_key="tooshort"))
    assert any("shorter than" in v for v in violations)


def test_long_random_bootstrap_key_is_clean() -> None:
    violations = evaluate_static_posture(
        _settings(di_bootstrap_api_key="di_" + "a" * 40)
    )
    assert violations == []


def test_empty_bootstrap_key_is_clean() -> None:
    """Empty is the RECOMMENDED prod value — the CLI mints the first real key."""
    violations = evaluate_static_posture(_settings(di_bootstrap_api_key=""))
    assert violations == []


def test_missing_fingerprint_hmac_key_is_a_violation() -> None:
    violations = evaluate_static_posture(_settings(instance_fingerprint_hmac_key=""))
    assert any("INSTANCE_FINGERPRINT_HMAC_KEY" in v for v in violations)


def test_set_fingerprint_hmac_key_is_clean() -> None:
    violations = evaluate_static_posture(
        _settings(instance_fingerprint_hmac_key="a-deployment-scoped-secret")
    )
    assert not any("INSTANCE_FINGERPRINT_HMAC_KEY" in v for v in violations)


def test_access_audit_disabled_is_a_violation() -> None:
    violations = evaluate_static_posture(_settings(access_audit_enabled=False))
    assert any("ACCESS_AUDIT_ENABLED" in v for v in violations)


def test_access_audit_non_strict_is_a_violation() -> None:
    violations = evaluate_static_posture(_settings(access_audit_strict=False))
    assert any("ACCESS_AUDIT_STRICT" in v for v in violations)


def test_access_audit_disabled_does_not_also_flag_non_strict() -> None:
    """When audit is off entirely, the strict-mode check is moot — only one violation, not two."""
    violations = evaluate_static_posture(
        _settings(access_audit_enabled=False, access_audit_strict=False)
    )
    assert len(violations) == 1


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


def test_dev_env_escapes_every_guard() -> None:
    """DI_ENV=dev is documented as escaping all checks — see the module docstring's scope note."""
    settings = _settings(di_env="dev", rls_enabled=False, auth_enabled=False,
                         access_audit_strict=False)
    assert_production_posture(settings)  # must not raise
