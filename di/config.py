"""Application settings — env-driven via pydantic-settings.

Single source of truth for configuration. Read once via ``get_settings()`` (cached).
No secrets are hard-coded; everything comes from the environment / ``.env``.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # --- App ---
    app_name: str = "document-intelligence"
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

    # --- Gate / pipeline ---
    gate_default_open: bool = True
    classifier_confidence_floor: float = 0.55
    masking_enabled_default: bool = False

    # --- Chunking / embeddings ---
    chunk_max_tokens: int = 512
    chunk_overlap_tokens: int = 64
    embedding_dim_default: int = 768
    embedding_batch_size: int = 32
    arep_async: bool = True

    # --- Languages / jurisdictions ---
    supported_languages: tuple[str, ...] = ("en", "es")
    supported_jurisdictions: tuple[str, ...] = ("US", "CA", "MX")

    # --- Object storage (optional) ---
    s3_enabled: bool = False
    s3_endpoint: str = ""
    s3_bucket: str = "document-intelligence"
    s3_access_key: str = ""
    s3_secret_key: str = ""

    @property
    def has_azure_vision(self) -> bool:
        return bool(self.azure_vision_endpoint and self.azure_vision_key)

    @property
    def qualified_schema(self) -> str:
        """Double-quoted schema identifier for safe interpolation into SQL."""
        return f'"{self.pg_schema}"'


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
