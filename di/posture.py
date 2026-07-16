"""Static production-posture guard.

Misconfiguration of a production instance must crash the process, not degrade silently — a
refuse-ready (`/readyz` 503) still lets the socket answer direct/internal calls, which is exactly
the fail-open behavior this closes. Everything in this module is knowable from `Settings` alone,
so `assert_production_posture` can run before `FastAPI()` is even constructed, guaranteeing a
misconfigured production deploy never serves a single request.

Runtime-observed checks that need a live DB connection (is the connected role actually
non-superuser? do the RLS policies actually exist?) live in `di.db.assert_rls_posture` — this
module only ever inspects settings. That function ALSO covers what an earlier design draft called
a separate "DB-side superuser/BYPASSRLS check" — there is only one such check in this codebase,
not two; auth posture below adds its own static (settings-only) violations to the same list.

This module is grown, not replaced, as each phase adds its own static checks:
`evaluate_static_posture` is the one place violations accumulate.

Scope note: only ``settings.is_production`` (``DI_ENV`` in staging|prod|production) is guarded.
``DI_ENV=dev`` deliberately escapes every check here — it is meant for engineers iterating against
a shared environment, not for holding real tenant data. If a deployment's "dev" environment ever
holds real KYC data, treat it as production for this guard's purposes (set ``DI_ENV=staging``).
"""
from __future__ import annotations

from di.config import Settings, get_settings

#: The literal bootstrap key baked into docker-compose.yml / tools/smoke_test.py. A production
#: deployment that still has this value is definitely not deployed correctly.
_DEMO_BOOTSTRAP_KEY = "di_local_dev_key_change_me"
_MIN_BOOTSTRAP_KEY_LENGTH = 32


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
    if not settings.auth_enabled:
        violations.append(
            "AUTH_ENABLED is false — every /api/v1 route would be unauthenticated"
        )
    if not settings.mask_by_default:
        violations.append(
            "MASK_BY_DEFAULT is false — sensitive values would be served unmasked unless every "
            "caller explicitly opts into masking"
        )
    if settings.di_bootstrap_api_key:
        if settings.di_bootstrap_api_key == _DEMO_BOOTSTRAP_KEY:
            violations.append(
                "DI_BOOTSTRAP_API_KEY is still the documented demo value — unset it and mint the "
                "first real key via `python -m di.tools.keys create`"
            )
        elif len(settings.di_bootstrap_api_key) < _MIN_BOOTSTRAP_KEY_LENGTH:
            violations.append(
                f"DI_BOOTSTRAP_API_KEY is shorter than {_MIN_BOOTSTRAP_KEY_LENGTH} characters — "
                "too weak for production; unset it and mint the first key via "
                "`python -m di.tools.keys create` instead of a long-lived wildcard env secret"
            )
    if not settings.access_audit_enabled:
        violations.append(
            "ACCESS_AUDIT_ENABLED is false — read-side access to tenant PII would leave no audit "
            "trail at all"
        )
    elif not settings.access_audit_strict:
        violations.append(
            "ACCESS_AUDIT_STRICT is false — a stalled audit writer would silently DROP access "
            "records instead of refusing the read; production must accept 'no audit -> no reads' "
            "explicitly (set ACCESS_AUDIT_STRICT=true)"
        )
    if settings.ingest_embedded_worker:
        violations.append(
            "INGEST_EMBEDDED_WORKER is true — production API replicas would run ingest/OCR work "
            "in-process, recreating the coupling the durable-queue upgrade exists to remove; "
            "deploy dedicated `python -m di.worker` processes and set this false"
        )
    if settings.blob_backend == "none":
        violations.append(
            "BLOB_BACKEND is 'none' — async (202) ingest requires durable blob storage "
            "(blob-at-accept: the payload must survive between the 202 and the worker claiming "
            "it); set BLOB_BACKEND to postgres or s3, or restrict ingest to ?stream=true"
        )
    elif settings.blob_backend == "local":
        violations.append(
            "BLOB_BACKEND is 'local' — a worker on a different node/pod than the accepting API "
            "replica would get BlobNotFound for every job (node-local disk, no shared RWX "
            "volume); use postgres or s3 for any multi-node deployment"
        )
    if not settings.instance_fingerprint_hmac_key:
        violations.append(
            "INSTANCE_FINGERPRINT_HMAC_KEY is unset — multi-valued-fact instance fingerprints "
            "(director/beneficial-owner identities) would use an unsalted SHA-256, letting anyone "
            "with API access dictionary-confirm a masked value's identity; set a deployment-scoped "
            "key from Secret Manager (rotation is unsupported — rotating it orphans every existing "
            "adjudication keyed on the old fingerprints)"
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
