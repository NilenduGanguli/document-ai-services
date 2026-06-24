# Document Intelligence

Unified document-intelligence platform for banking KYC. It turns a client's documents
(PDF / DOCX / JPEG / PNG) into a **versioned, per-client knowledge tree** and serves it to
downstream services via an API for search, single-document Q&A, and structured fact retrieval —
PII-safe throughout.

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
The app applies migrations on startup and — because the compose DB ships pgvector — creates the
`vector` extension, embedding columns, and HNSW indexes (the local Homebrew Postgres lacks
pgvector, so a bare `pytest`/uvicorn run degrades to FTS-only search). Point at the real model
gateway by setting `RETRIEVAL_BASE_URL` and `DI_RETRIEVAL_STUB=false` in `docker-compose.yml`.

**Supported input formats** (offline, no Azure key): **PDF** (digital → pypdf text layer; scanned →
Tesseract via poppler), **DOCX** (python-docx), **PNG/JPEG** (Tesseract OCR), and plain text. In
production, Azure AI Vision Read handles PDF/images instead (set `AZURE_VISION_*`); Tesseract image
OCR is lossy (e.g. 0→O), so prefer Azure for images. Generate real fixtures with
`python tools/make_samples.py` → `samples/generated/{passport.pdf, ssn_card.docx,
ine_credencial.png, utility_bill.jpg}`.

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
