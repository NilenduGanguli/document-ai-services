"""Application settings — env-driven via pydantic-settings.

Single source of truth for configuration. Read once via ``get_settings()`` (cached).
No secrets are hard-coded; everything comes from the environment / ``.env``.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class _FileSecretSource(PydanticBaseSettingsSource):
    """Read a field's value from a file when ``<ENV_NAME>_FILE`` is set.

    Maps cleanly onto how secrets actually arrive in production: GCP Secret Manager via Cloud
    Run's ``--set-secrets`` writes a mounted file (or, more commonly on this team, injects the
    plain env var directly — both work, this covers the file-mount case); Kubernetes secret
    volumes and Vault agent sinks are files by convention. Lower priority than a literal env var
    (see ``settings_customise_sources``) so ``PG_PASSWORD=x PG_PASSWORD_FILE=/secrets/pg``
    resolves predictably to the explicit value.
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        env_name = (field.alias or field_name).upper()
        path = os.environ.get(f"{env_name}_FILE")
        if not path:
            return None, field_name, False
        try:
            return Path(path).read_text(encoding="utf-8").strip(), field_name, False
        except OSError as exc:
            raise ValueError(f"{env_name}_FILE={path!r} could not be read: {exc}") from exc

    def __call__(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_name, field in self.settings_cls.model_fields.items():
            value, key, _ = self.get_field_value(field, field_name)
            if value is not None:
                result[key] = value
        return result


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings,
    ):
        # Precedence (highest first): explicit init kwargs > real env vars > _FILE indirection >
        # .env file > pydantic-settings' own secrets_dir mechanism (unused here, kept for parity).
        return (init_settings, env_settings, _FileSecretSource(settings_cls), dotenv_settings,
                file_secret_settings)

    # --- App ---
    app_name: str = "document-ai-services"
    di_env: str = "local"                    # local | dev | staging | prod
    di_log_level: str = "INFO"
    di_executor_workers: int = 32

    # --- Postgres ---
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = ""
    pg_database: str = "document_intelligence"
    pg_schema: str = "di"
    pg_pool_min: int = 2
    pg_pool_max: int = 16
    pg_hash_partitions: int = 64
    rls_enabled: bool = True

    # --- Migrations / roles ---
    # Owner-class role used ONLY for schema DDL (migrations). Empty falls back to pg_user, which
    # keeps bare local/unit-test runs working with a single role. In production this should be a
    # distinct role so the runtime pool never holds DDL-capable credentials — see MIGRATIONS_MODE.
    pg_migration_user: str = ""
    pg_migration_password: str = ""
    # Worker-role credentials (di_worker_login) for the queue's cross-tenant claim/heartbeat/reap
    # operations — see di.db.acquire_queue(). Empty falls back to pg_user, so a bare single-role
    # local/test setup keeps working. A dedicated `python -m di.worker` process typically sets
    # PG_USER to di_worker_login directly and can leave these unset; the embedded (compose/demo)
    # worker needs them set distinctly, since the API's own pool must stay on di_app.
    pg_worker_user: str = ""
    pg_worker_password: str = ""
    # auto = apply migrations in-process as the migration role (compose/demo default); verify =
    # never apply, only assert the ledger matches what's on disk and refuse to boot on drift (the
    # migration step runs separately via `python -m di.migrate`); off = skip entirely (advanced).
    migrations_mode: str = "auto"
    # Path to a CA bundle for verify-full TLS to Postgres. Empty = asyncpg's default (sslmode
    # "prefer" if the server offers it, unverified). Recommended for any non-local environment.
    pg_ssl_root_cert: str = ""

    # --- Retrieval service (model gateway) ---
    retrieval_base_url: str = "http://localhost:8000"
    retrieval_api_key: str = ""
    retrieval_timeout: float = 120.0
    di_retrieval_stub: bool = False

    # --- Azure AI Vision Read (OCR) ---
    azure_vision_endpoint: str = ""
    azure_vision_key: str = ""

    # --- MCP endpoint (agent-facing tool surface; mounts on the same app/container) ---
    # When true, di.app mounts a Model Context Protocol server at /mcp so other agents can call
    # the read/search/ingest tools. It reuses the same X-API-KEY auth and per-tenant RLS as the
    # REST API — no new isolation surface. Set false to omit the mount entirely.
    mcp_enabled: bool = True

    # --- AuthN / AuthZ ---
    # Disable ONLY for a local demo; every /api/v1 route is unauthenticated when false.
    auth_enabled: bool = True
    # Seeds a wildcard key at startup so a fresh container is usable from env alone. Production
    # must leave this unset — see di/posture.py — and mint its first key via `python -m di.tools.keys`.
    di_bootstrap_api_key: str = ""
    key_rotation_overlap_hours: int = 24

    # --- Rate limiting (per-process backstop; exact global limits belong to the bank's gateway) ---
    rate_limit_enabled: bool = True
    rate_limit_default_rps: float = 50.0
    rate_limit_burst: int = 100
    # Failed-auth (unknown/invalid key) backstop: a short negative-result cache so a credential-
    # stuffing flood cannot hammer di_api_key with one uncacheable lookup per request.
    auth_failure_cache_seconds: float = 5.0

    # --- Per-tenant ingest quotas (admission-time fairness; di_tenant_policy overrides per client) ---
    ingest_max_active_jobs_per_client: int = 25
    ingest_daily_limit_per_client: int = 0     # 0 = unlimited

    # --- Read-side access audit ---
    access_audit_enabled: bool = True
    # Prod posture requires this true (see di/posture.py): a stalled writer then 503s reads rather
    # than silently dropping audit records — "no audit -> no reads" is a deliberate compliance
    # trade a bank must accept explicitly. Local/dev default false so a slow/absent DB never turns
    # a demo into an outage.
    access_audit_strict: bool = False
    access_audit_queue_max: int = 10_000
    access_audit_batch: int = 500
    access_audit_flush_ms: int = 1000
    access_audit_retention_days: int = 400
    access_audit_partition_months_ahead: int = 3

    # --- Gate / pipeline ---
    gate_default_open: bool = True
    classifier_confidence_floor: float = 0.55
    # Server-side default for the serving masking projection. Fail-closed: sensitive values are
    # redacted unless the caller explicitly (and with clearance) asks for them.
    mask_by_default: bool = True
    ingest_concurrency: int = 4              # max ingest jobs processed at once per worker process

    # --- Durable job queue / workers ---
    # Defaults to `not is_production` via the validator below when left unset (env, init kwarg,
    # and .env all count as "set") — a forgotten env var in prod must never silently recreate the
    # old in-process coupling of ingest work to API replicas.
    ingest_embedded_worker: bool = True
    job_lease_seconds: int = 300             # > worst-case OCR stage; heartbeat renews at lease/4
    job_max_attempts: int = 3
    job_retry_base_seconds: float = 30.0
    job_retry_max_seconds: float = 3600.0
    job_poll_interval_seconds: float = 2.0   # LISTEN fallback when a NOTIFY is missed
    job_claim_batch: int = 4
    job_reaper_interval_seconds: float = 30.0
    job_drain_timeout_seconds: float = 30.0
    worker_metrics_port: int = 9090
    # Fleet-wide per-tenant concurrency cap (soft — see di.jobs.claim). The global queued cap from
    # the original design is dropped: the accept path runs under the tenant GUC (FORCE RLS), so a
    # cross-tenant count would need a SECURITY DEFINER function or a dedicated role for no real
    # gain — per-tenant caps plus the per-key rate limiter plus worker-side depth alerts cover it.
    ingest_tenant_max_running: int = 4
    ingest_tenant_max_queued: int = 50_000   # accept-side backpressure -> 429
    blob_retain_after_ingest: bool = True
    # Dead-job payload blobs (content-addressed uploads for jobs that never succeeded) are swept
    # by di.tools.blob_gc after this many days — see di.jobs.enqueue(kind="blob_gc").
    dead_blob_retention_days: int = 30

    @model_validator(mode="after")
    def _embedded_worker_default_off_in_prod(self) -> Settings:
        if "ingest_embedded_worker" not in self.model_fields_set and self.is_production:
            self.ingest_embedded_worker = False
        return self

    # --- Request limits (unbounded requests/responses are a DoS + gateway-timeout risk) ---
    max_upload_mb: int = 25
    max_top_k: int = 100
    default_page_size: int = 50
    max_page_size: int = 200

    # --- Chunking / embeddings ---
    chunk_max_tokens: int = 512
    chunk_overlap_tokens: int = 64
    embedding_dim_default: int = 768
    embedding_batch_size: int = 32
    arep_async: bool = True

    # --- Languages / jurisdictions ---
    supported_languages: tuple[str, ...] = ("en", "es")
    supported_jurisdictions: tuple[str, ...] = ("US", "CA", "MX")

    # --- Ontology ---
    # Stamped into provenance + merged facts so a fact's vintage is identifiable after changes.
    # NOTE: bumping this default is a no-op wherever ONTOLOGY_VERSION is pinned in the environment
    # (env vars always win over the default) — vintage auditability relies on deployments actually
    # rolling the env value forward, not on this default alone.
    ontology_version: str = "1.1.0"
    # Deployment-scoped HMAC key for multi-valued-fact instance fingerprints (see
    # di.subtree.merge.instance_fingerprint). Unsalted SHA-256 lets anyone with API access
    # dictionary-confirm a low-entropy value (a director's name) against a masked response; HMAC
    # closes that inference channel. Rotation is deliberately unsupported in v1 — rotating this
    # key changes every existing instance_key, silently orphaning every adjudication keyed on the
    # old fingerprints. di.posture requires this set in production.
    instance_fingerprint_hmac_key: str = ""

    # --- Blob storage: where the raw uploaded bytes live ---
    # postgres = bytea in di_blob | local = filesystem/docker volume | s3 = S3/MinIO | none = don't retain
    blob_backend: str = "postgres"
    blob_local_dir: str = "/data/blobs"
    s3_endpoint: str = ""                    # set for MinIO/S3-compatible; empty = real AWS
    s3_bucket: str = "document-intelligence"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_prefix: str = "documents"

    @field_validator("blob_backend")
    @classmethod
    def _valid_blob_backend(cls, v: str) -> str:
        allowed = {"postgres", "local", "s3", "none"}
        if v not in allowed:
            raise ValueError(f"blob_backend must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("migrations_mode")
    @classmethod
    def _valid_migrations_mode(cls, v: str) -> str:
        allowed = {"auto", "verify", "off"}
        if v not in allowed:
            raise ValueError(f"migrations_mode must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("azure_vision_endpoint", "retrieval_base_url")
    @classmethod
    def _sane_url(cls, v: str) -> str:
        """Reject malformed egress URLs at startup rather than at first request."""
        if not v:
            return v
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"must be an http(s) URL with a host, got {v!r}")
        return v

    @property
    def has_azure_vision(self) -> bool:
        return bool(self.azure_vision_endpoint and self.azure_vision_key)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.di_env.lower() in ("staging", "prod", "production")

    @property
    def qualified_schema(self) -> str:
        """Double-quoted schema identifier for safe interpolation into SQL."""
        return f'"{self.pg_schema}"'


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
