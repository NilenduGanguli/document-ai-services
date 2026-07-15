"""Static production-posture guard.

Misconfiguration of a production instance must crash the process, not degrade silently — a
refuse-ready (`/readyz` 503) still lets the socket answer direct/internal calls, which is exactly
the fail-open behavior this closes. Everything in this module is knowable from `Settings` alone,
so `assert_production_posture` can run before `FastAPI()` is even constructed, guaranteeing a
misconfigured production deploy never serves a single request.

Runtime-observed checks that need a live DB connection (is the connected role actually
non-superuser? do the RLS policies actually exist?) live in `di.db.assert_rls_posture` — this
module only ever inspects settings.

This module is grown, not replaced, as later phases add their own static checks (auth posture in
the auth-hardening phase): `evaluate_static_posture` is the one place violations accumulate.
"""
from __future__ import annotations

from di.config import Settings, get_settings


def evaluate_static_posture(settings: Settings) -> list[str]:
    """Return every posture violation for ``settings``. Empty list means clean.

    Pure and unconditional (does not itself check ``is_production``) so every branch is trivially
    unit-testable; callers gate on ``is_production`` themselves (see
    :func:`assert_production_posture`).
    """
    violations: list[str] = []
    if not settings.rls_enabled:
        violations.append(
            "RLS_ENABLED is false — the headline tenant-isolation control would be inert"
        )
    if settings.migrations_mode == "auto" and not (
        settings.pg_migration_user and settings.pg_migration_user != settings.pg_user
    ):
        violations.append(
            "MIGRATIONS_MODE=auto with no distinct PG_MIGRATION_USER — production instances "
            "should not hold DDL-capable credentials; set MIGRATIONS_MODE=verify and run "
            "`python -m di.migrate` as a separate deploy step, or configure a distinct "
            "PG_MIGRATION_USER for in-process migration"
        )
    return violations


def assert_production_posture(settings: Settings | None = None) -> None:
    """Raise ``RuntimeError`` naming every violation when ``settings.is_production`` and any exist.

    Call this as the very first statement of ``create_app()`` — before ``FastAPI()`` is
    constructed — so a misconfigured production deploy fails at boot instead of serving degraded.
    A plain ``RuntimeError`` propagating out of a normal (non-lifespan) call site is the reliable
    signal here; this function never touches the network or the event loop.
    """
    settings = settings or get_settings()
    if not settings.is_production:
        return
    violations = evaluate_static_posture(settings)
    if violations:
        raise RuntimeError(
            f"refusing to start in production (DI_ENV={settings.di_env}) — "
            "posture violations: " + "; ".join(violations)
        )
