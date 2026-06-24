# Retrieval Framework — API Additions Required by Document Intelligence

> **Purpose:** This document specifies endpoints the **`retrieval`** service must expose so the
> new **`document_intelligence`** service can reuse retrieval as the single gateway for all model
> access (embeddings, LLM, rerank) instead of re-implementing the Stellar/Vertex + COIN auth stack.
> Hand this to the agent updating the `retrieval` repo.
>
> **Constraints for the implementer:**
> - **Additive & backward-compatible** — do NOT change existing endpoint contracts
>   (`/api/retrieve`, `/api/ingest/*`, `/api/kyc/*`, `/api/documents/*`).
> - Reuse the existing provider-agnostic surface: `get_stellar()` / `model_for()` /
>   `StellarClient`/`VertexClient` (`backend/app/stellar_client.py`). Do not bypass it.
> - **Service-to-service auth:** protect every new endpoint with the existing `X-API-KEY`
>   middleware (same key scheme as `/api/v1/*`). Document the key requirement.
> - **Embedding dim must be stable** and reported (gte-large-en-v1.5 = 1024-D; Vertex = 768-D).
>   Never silently switch providers within a deployment — document_intelligence locks its vector
>   columns to the dim returned here.
> - Run sync SDK calls via the existing executor pattern; keep latency/batch limits documented.

---

## Required

### 1. `POST /api/embed` — batch text embeddings  **[REQUIRED]**
The single most important addition. document_intelligence embeds its own `knode.content` and
`arep.rep_text` rows and stores the vectors in its **own** per-client pgvector index.

**Request**
```json
{ "texts": ["...", "..."], "task": "embedding", "model": null }
```
- `texts`: 1..N strings (support batches of at least 64; document the max).
- `task`: optional, defaults to `"embedding"` (routes through `model_for("embedding")`).
- `model`: optional explicit override.

**Response**
```json
{ "provider": "stellar", "model": "gte-large-en-v1.5", "dim": 1024,
  "vectors": [[0.01, ...], [0.02, ...]] }
```
- `vectors`: list-of-lists, same order as `texts` (reuse `StellarClient.embed`).
- `dim`, `model`, `provider` must be returned so the caller can verify dim stability.

### 2. `POST /api/llm/complete` — generic LLM completion via the gateway  **[REQUIRED]**
So document_intelligence can do classification, attribute extraction, summaries, hypothetical
questions, propositions, table/figure descriptions, and context prefixes WITHOUT its own Stellar
COIN auth / SSL cert wiring.

**Request**
```json
{ "task": "final_gen", "model": null,
  "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
  "temperature": 0, "max_tokens": 1024, "response_format": "json" }
```
- `task`: one of `final_gen | fast | rerank | contextual` → resolved via `model_for(task)`.
- `model`: optional explicit override (else from `task`).
- `response_format`: `"text"` (default) or `"json"` (hint the model to emit a single JSON object).

**Response**
```json
{ "text": "...", "model": "Llama-4-Scout-17B-16E-Instruct",
  "usage": {"prompt": 1234, "completion": 567, "total": 1801} }
```
- Reuse `StellarClient.chat(...)`; return `(text, TokenUsage)`.

**2b. `POST /api/llm/stream` — streamed variant (SSE) [RECOMMENDED]** — same request; emits `token`
deltas then a final `done` with usage. Mirrors `StellarClient.chat_stream`. Needed only if
document_intelligence streams generation to its own callers; not required for ingest.

---

## Recommended

### 3. `POST /api/rerank` — listwise / cross-encoder rerank  **[RECOMMENDED]**
Reuse retrieval's existing rerank stage so document_intelligence's hybrid search over
`knode`/`arep` doesn't re-implement it.

**Request**
```json
{ "query": "...", "candidates": [{"id": "n1", "text": "..."}], "top_k": 20, "model": null }
```
**Response**
```json
{ "ranked": [{"id": "n1", "score": 0.93}, {"id": "n7", "score": 0.81}] }
```
- `model` defaults to `model_for("rerank")`.

### 4. `GET /api/models` — model + capability discovery  **[RECOMMENDED]**
So document_intelligence self-configures rather than hardcoding.

**Response**
```json
{ "provider": "stellar", "embedding_model": "gte-large-en-v1.5", "embedding_dim": 1024,
  "tasks": { "final_gen": "Llama-4-Scout-17B-16E-Instruct", "fast": "Meta-Llama-3.1-8B-Instruct",
             "rerank": "Meta-Llama-3.3-70B-Instruct", "contextual": "Llama-4-Scout-17B-16E-Instruct" } }
```

---

## Optional

### 5. `POST /api/contextual-prefix` — context-prefix generation  **[OPTIONAL]**
Mirrors `pipeline/contextual.py`: given the full document text + a target chunk, return the
Anthropic-style 50–100 token situating prefix. Can instead be done via `/api/llm/complete`;
include only if a dedicated, prompt-tuned endpoint is preferred.

### 6. Optional metadata filter on `POST /api/retrieve`  **[OPTIONAL / FUTURE]**
Add an optional `filter` to the existing request (kept fully backward-compatible — absent ⇒
current behavior):
```json
{ "query": "...", "strategy": {...}, "filter": { "document_names": ["..."], "metadata": {"...":"..."} } }
```
Only needed if document_intelligence later delegates cross-corpus search to retrieval. In v1 it
keeps its own per-client scoped index, so this is **future**, not blocking.

---

## Not needed from retrieval
- Per-client knowledge-tree storage, `ltree`, the classifier/PII gate, OCR, deterministic
  extraction, and the per-client scoped multi-vector index all live in **document_intelligence**.
- document_intelligence does **not** write to retrieval's corpus tables.

## Summary checklist for the retrieval-repo agent
- [ ] `POST /api/embed` (batch, returns vectors+dim+model) — **required**
- [ ] `POST /api/llm/complete` (task/model, messages, json mode, usage) — **required**
- [ ] `POST /api/llm/stream` (SSE) — recommended
- [ ] `POST /api/rerank` — recommended
- [ ] `GET /api/models` — recommended
- [ ] `POST /api/contextual-prefix` — optional
- [ ] `filter` on `POST /api/retrieve` — optional/future
- [ ] All behind `X-API-KEY`; additive; embedding dim reported & stable.
