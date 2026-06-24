#!/usr/bin/env sh
# Wait for Postgres, then start the app. Migrations (and pgvector embedding-column bootstrap)
# run idempotently inside the app's startup lifespan.
set -e

echo "[entrypoint] waiting for Postgres at ${PG_HOST:-db}:${PG_PORT:-5432} ..."
python - <<'PY'
import os, socket, time, sys
host = os.environ.get("PG_HOST", "db")
port = int(os.environ.get("PG_PORT", "5432"))
for _ in range(60):
    try:
        with socket.create_connection((host, port), timeout=2):
            print("[entrypoint] Postgres reachable.")
            sys.exit(0)
    except OSError:
        time.sleep(1)
sys.exit("[entrypoint] ERROR: Postgres not reachable")
PY

echo "[entrypoint] starting: $*"
exec "$@"
