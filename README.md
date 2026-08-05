# Document AI Services

Document AI Services — a platform for banking KYC. It turns a client's documents
(PDF / DOCX / JPEG / PNG) into a **versioned, per-client knowledge tree** and serves it to
downstream services via an API for search, single-document Q&A, and structured fact retrieval —
PII-safe throughout.

- **Console:** a React (Vite + TS) SPA compiled into `frontend/dist` and served by FastAPI itself —
  dashboard/readiness, drag-drop ingest with live pipeline stages, knowledge tree with a provenance
  drawer, merged facts, hybrid search, jobs, and an admin/erasure page.
- **Ingest is async:** `POST /api/v1/ingest` returns **202 + `job_id`**; poll `GET /api/v1/jobs/{id}`
  for live stages and the terminal outcome. A dropped connection no longer loses the work.
  `?stream=true` keeps the original SSE-on-one-connection behaviour.
- **Geography / languages:** US, Canada, Mexico · English + Spanish.
- **The star:** a per-document **knowledge subtree** — `knode` (returnable nodes) + `arep`
  (multi-vector accessibility representations) — that is semantically configured, logically
  linked, contextually enriched, and accessibility-enhanced, with provenance + verification status.
- **Model access** (embeddings + LLM + rerank) is delegated to the existing **`retrieval`**
  service (no Stellar/COIN credentials live here). See
  [reports/retrieval-api-requirements.md](reports/retrieval-api-requirements.md).

## Docs
- Design spec: [docs/specs/2026-06-24-document-intelligence-design.md](docs/specs/2026-06-24-document-intelligence-design.md)
- Requirements & decision log: [reports/requirements-and-interpretation.md](reports/requirements-and-interpretation.md)
- Retrieval API additions (for the retrieval-repo team): [reports/retrieval-api-requirements.md](reports/retrieval-api-requirements.md)

## Develop
```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev,extract]"      # core + tests; add ",ml" for the classifier/PII stack
cp .env.example .env                     # fill PG_*, RETRIEVAL_*, AZURE_VISION_* as needed
ruff check di tests
pytest -q                                # pure-logic tests run without a DB; mark-gated otherwise
```

`DI_RETRIEVAL_STUB=true` runs against an in-process fake of the retrieval model gateway
(zero-vectors + echo completions) so the pipeline is exercisable without the live service.

## Run as a container (full stack, with pgvector)
```bash
docker compose up --build        # Postgres-with-pgvector + the app on http://localhost:8080
```
The image is **multi-stage**: it compiles the React console with Vite and copies only the built
`dist` into a lean Python runtime — no `node_modules`, and no pre-built artifact to commit.
Open **http://localhost:8080** for the console; the API is under `/api/v1` and the OpenAPI docs at
`/docs`. Host ports are overridable via `DI_APP_PORT` / `DI_DB_PORT` in a local `.env` if 8080/5433
are taken on your machine.

**Auth is on by default.** The compose stack seeds `DI_BOOTSTRAP_API_KEY` at startup; paste that key
into the console's header bar (or send it as `X-API-KEY`). Set `AUTH_ENABLED=false` only for a
throwaway local demo — it leaves every route open.

### Where the raw document bytes live
Every upload is written to the configured blob store **before** the `202` is returned and before
the `di_job` row exists (*blob-at-accept*) — a crash between accepting a document and a worker
claiming it can never lose the payload the caller was told was accepted. Pick the backend with
`BLOB_BACKEND`; the pipeline, deletion and tenant-purge paths all work identically across the four:
```bash
docker compose up --build                  # postgres  — bytea in di_blob (default)
BLOB_BACKEND=local docker compose up       # local     — a docker volume at /data/blobs
BLOB_BACKEND=s3 docker compose --profile s3 up --build
                                           # s3        — MinIO (console at :9001); use a real
                                           #             bucket by setting S3_ENDPOINT/S3_BUCKET
BLOB_BACKEND=none docker compose up        # none      — do not retain raw bytes at all
```
The `s3` line needs **both** halves: `--profile s3` starts the bundled MinIO (bucket auto-created
by `minio-init`), `BLOB_BACKEND=s3` points the app at it. Objects land at
`s3://$S3_BUCKET/$S3_PREFIX/<client_id>/<sha256>/<filename>` — the tenant prefix is applied by the
store, never taken from the key, so one tenant can never read another's object by presenting its
URI.

**Where the location is recorded.** The URI is persisted, not just logged: `di_job.payload` carries
it from accept to worker claim, then `di_documents.blob_uri` / `blob_backend` record it on the
document row and `doc_version.blob_uri` / `blob_backend` (migration 012) pin it per immutable
version — the document row is upserted per logical document, so only the version row survives being
superseded. Authorized callers see it on `GET /api/v1/clients/{client_id}/documents`. Production
posture rejects `none` and `local` (see `di/posture.py`).

### Verify it end to end
```bash
python tools/smoke_test.py                 # 47 checks: auth, async ingest, masking, erasure, ...
```

### Ops surfaces
| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness only — the process is up |
| `GET /readyz` | Per-dependency readiness (db, migrations, pgvector, retrieval, blob, ocr, auth); **503** when a required component is down |
| `GET /metrics` | Prometheus: gate decisions, **LLM egress**, OCR engine, stage timings, ingest outcomes |
The app applies migrations on startup and — because the compose DB ships pgvector — creates the
`vector` extension, embedding columns, and HNSW indexes (the local Homebrew Postgres lacks
pgvector, so a bare `pytest`/uvicorn run degrades to FTS-only search). Point at the real model
gateway by setting `RETRIEVAL_BASE_URL` and `DI_RETRIEVAL_STUB=false` in `docker-compose.yml`.

**Supported input formats:** **PDF** (digital → pypdf text layer; scanned → OCR), **DOCX**
(python-docx), **PNG/JPEG** (OCR), and plain text. Generate real fixtures with
`python tools/make_samples.py` → `samples/generated/{passport.pdf, ssn_card.docx,
ine_credencial.png, utility_bill.jpg}`.

**Image / scanned-PDF OCR — Azure Computer Vision Read v3.2.** The app talks to the Azure Read v3.2
REST API directly over `httpx` (`di/ocr/vision.py`) — **no Azure SDK in any image**. The compose
stack ships a **mock Azure OCR container** (`mock_azure_ocr/`, plain Python + Tesseract, no SDK)
that serves the *same* v3.2 contract (`POST /vision/v3.2/read/analyze` → `Operation-Location` →
`GET …/analyzeResults/{id}`), so the Azure code path runs end-to-end **offline** — images report
`engine: azure-vision-read`. Point at **real Azure** by overriding the env (no code change):
```bash
AZURE_VISION_ENDPOINT=https://<resource>.cognitiveservices.azure.com/ \
AZURE_VISION_KEY=<key> docker compose up -d app
```
If `AZURE_VISION_*` is unset entirely, OCR falls back to local Tesseract.

**Exercise every flow + generate a report:**
```bash
DI_BASE_URL=http://localhost:8080 python tools/flow_report.py
# writes reports/local-flow-test-report.md (ingest SSE, tree, masking, facts, search,
# manifest, answerable-questions, provenance, version deltas) using the samples/ fixtures.
```
Tear down: `docker compose down -v` (and `colima stop` to halt the VM).

## Layout
See the design spec §10. Foundation: `di/config.py`, `di/db.py`, `di/retrieval_client.py`,
`di/models.py`, `di/ontology.py`, `di/app.py`, `di/migrations/`. Modules: `di/ocr`, `di/gate`,
`di/extract`, `di/subtree`, `di/routers`.
