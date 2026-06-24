-- 001_extensions.sql — required Postgres extensions. Idempotent.
-- `__SCHEMA__` is rewritten to the configured schema by di/db.py:run_migrations().
--
-- pgvector (the `vector` type, embedding columns, HNSW indexes) is intentionally NOT created
-- here: di/db.py adds the embedding columns + HNSW indexes at runtime once the live embedding
-- dimension is known, and skips them cleanly when pgvector is not installed (e.g. local dev).

CREATE EXTENSION IF NOT EXISTS ltree SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto SCHEMA public;
