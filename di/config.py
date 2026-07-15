"""Application settings — env-driven via pydantic-settings.

Single source of truth for configuration. Read once via ``get_settings()`` (cached).
No secrets are hard-coded; everything comes from the environment / ``.env``.
"""
from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # --- App ---
    app_name: str = "document-intelligence"
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

    # --- Retrieval service (model gateway) ---
    retrieval_base_url: str = "http://localhost:8000"
    retrieval_api_key: str = ""
    retrieval_timeout: float = 120.0
    di_retrieval_stub: bool = False

    # --- Azure AI Vision Read (OCR) ---
    azure_vision_endpoint: str = ""
    azure_vision_key: str = ""

    # --- AuthN / AuthZ ---
    # Disable ONLY for a local demo; every /api/v1 route is unauthenticated when false.
    auth_enabled: bool = True
    # Seeds a wildcard key at startup so a fresh container is usable from env alone.
    di_bootstrap_api_key: str = ""

    # --- Gate / pipeline ---
    gate_default_open: bool = True
    classifier_confidence_floor: float = 0.55
    # Server-side default for the serving masking projection. Fail-closed: sensitive values are
    # redacted unless the caller explicitly (and with clearance) asks for them.
    mask_by_default: bool = True
    ingest_concurrency: int = 4              # max ingest jobs processed at once per instance

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
    ontology_version: str = "1.0.0"

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
