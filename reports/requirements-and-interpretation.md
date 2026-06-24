# Document Intelligence — Requirements & Interpretation Log

> Living record of what the product owner (Nilendu) has asked for, how I (the engineering
> agent) interpret it, and the design that follows. Append-only changelog at the bottom.
> If anything here misstates intent, the product owner's correction wins — update this file.

**Status:** Brainstorming / design (no implementation started).
**Repo target:** `~/document_intelligence` (standalone, vendoring shared `retrieval` infra).
**Last updated:** 2026-06-24

---

## 1. Product vision

A **unified document intelligence & document processing platform** for a bank. It turns a
client's KYC documents (PDF, DOCX, JPEG, PNG, …) into structured, queryable knowledge and
exposes it via an API to **downstream services** that search documents, ask questions, and
pull specific facts about a client. Separate system from the existing `retrieval` RAG Studio;
reuses its infra patterns. **The star is a novel per-document knowledge data structure.**

---

## 2. Locked decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | `client_id` **arrives with the document** | No entity resolution for the primary key in v1 |
| D2 | Scope = **extract → structure → serve** | No risk scoring / PEP / AML link analysis in v1 |
| D3 | **Standalone repo** at `~/document_intelligence`, vendoring shared libs | Matches "separate thing" intent |
| D4 | OCR = **Azure AI Vision Read (cloud)** | Flat text + per-line bbox + confidence; structure is reconstructed by us |
| D5 | **Hybrid**: provenance/RAG layer + derived knowledge tree | Grounding + traversable semantics |
| D6 | **PostgreSQL + pgvector** (`ltree`, partition/index by `client_id`, RLS) | Matches stack; "B+ tree by client_id" |
| D7 | **Full cross-document merge** — a client-level merged knowledge view across the client's docs | Owner choice; intra-client only (no cross-client) |
| D8 | **Accessibility reps = full set from day one** (all `arep` rep_types) | Owner choice; accept higher LLM ingest cost |
| D9 | **Geography = North America** (US + Canada [+ Mexico, pending Q-J]); **India dropped** from v1 | Owner choice |
| D10 | **Languages = English + Spanish** (French deferred) | Owner choice; Spanish ⇒ Mexico likely in scope |
| D11 | **Scale = millions of clients ⇒ HASH-partition by `client_id`** | Owner choice |
| D12 | **All model access (embed / LLM / rerank) routed through the `retrieval` service** via new endpoints (see [retrieval-api-requirements.md](retrieval-api-requirements.md)); document_intelligence keeps its OWN per-client scoped vector index | Owner: reuse retrieval as model gateway; another agent will add the endpoints. No COIN/VDI creds in document_intelligence |
| D13 | **All §3.9 capabilities in v1**, incl. access-aware masking/redaction — but **toggleable** (can be turned fully off) and **non-breaking** when on (all non-sensitive content stays fully accessible) | Owner choice |

---

## 3. Architecture (synthesized 2026-06-24 from research)

### 3.1 Unified tree = a path-encoded forest of per-document knowledge subtrees

There is ONE node table holding the whole forest; the **`ltree` path** encodes the hierarchy
the owner described:

```
client_<id> . doctype_<type> . v<n> . <section> . <subsection> . <chunk> . <fact>
   client        doc-type        ver    ...    the per-document knowledge subtree ...
```

- **Client root** — `client_id` + client-level metadata.
- **Document-type branch** — Passport, Aadhaar, Certificate of Incorporation, …
- **Version** — each re-upload of a similar document = a new immutable version.
- **Knowledge subtree** — one per document version (the novel structure, §3.3).

In addition to this per-document spine, a **client-level merged knowledge view** (decision D7)
hangs directly under the client root and consolidates facts ACROSS the client's documents:

```
client_<id>
├── _merged                     ← client-level merged knowledge view (the resurrected domains)
│   ├── Identity                  consolidated facts (e.g. DOB, full name) merged across docs
│   ├── Addresses                 with conflict resolution + provenance to source facts
│   ├── Ownership & Control
│   ├── Accounts & Products
│   ├── Employment & Income
│   └── …                         (domain ontology = client-level merged branches)
└── docs
    └── doctype_<type> . v<n> . <per-document knowledge subtree: knode + arep>
```

Merge mechanics (intra-client only): per-document `fact` knodes are grouped by a **canonical
attribute key** (e.g. `identity.date_of_birth`); a merged fact node holds the resolved value +
`source_fact_ids[]` (provenance to every contributing per-document fact) + a conflict flag.
**Conflict-resolution policy (D-K = confidence-weighted + flag):** highest extraction-confidence
source wins regardless of recency; disagreements flagged `needs_review`; all source values
retained with provenance. No cross-CLIENT merge.

### 3.2 Ingestion pipeline (one pass per document)

```
Upload(client_id, file)
  → OCR  (Azure AI Vision Read → text + per-line bbox + confidence; raw dump retained)
  → Stage 0  cheap gate: regex + anchor keywords + ID-checksum detection
  → Stage 1  LOCAL document-type classifier (rules → TF-IDF+LinearSVC → SetFit upgrade)   [no cloud egress]
  → Stage 2  PII / sensitivity scan (Presidio Analyzer) → sensitivity bucket {LOW..CRITICAL}
  → Stage 3  routing gate (config-driven): SEND_TO_LLM | REDACT_THEN_SEND | DETERMINISTIC_ONLY
  → Extraction:
       • LLM path (Stellar): base + LLM-chosen attribute KV facts            (allowed docs)
       • Deterministic path: strict per-doc-type schema from OCR dump          (gated docs)
  → Build knowledge subtree (knode) + accessibility representations (arep)
  → Version (dedup by hash, supersedes chain, current pointer, changed-fields diff)
  → SSE stage events to caller
```

**Gate policy for now:** OPEN (most docs may proceed to the LLM), but all policy hooks are
built so a later policy can restrict by doc-type / sensitivity. **Fail-safe:** an `UNKNOWN`
doc-type with any non-LOW sensitivity routes to `DETERMINISTIC_ONLY`, never auto-sends.
Sensitivity scan is authoritative and independent of the type classifier.

### 3.3 The knowledge subtree — TWO tables: `knode` (returned) + `arep` (searched)

The load-bearing idea ("index-many / return-parent"): every retrieval aid is a **row**, not a
schema change.

**`knode`** — canonical logical & content nodes (the things returned to a consumer):
`id, client_id, doc_id, version_id, parent_id, path (ltree), node_type
(document|section|chunk|table|figure|fact|summary), seq, depth, title, content,
content_tsv (generated FTS), context_prefix (Anthropic-style), content_embedding (runtime dim),
cross_refs[] (intra-doc DAG), entity_ids[] (entity links), provenance jsonb (page/bbox/offsets/
model versions), checksum_ok (for deterministic facts), confidence, token_count, created_at.`
Indexes: `gist(path)`, `gin(content_tsv)`, `hnsw(content_embedding)`, `(client_id, doc_id, version_id)`.

**`arep`** — accessibility representations (searched, never returned directly): one knode → many.
`id, knode_id, client_id, doc_id, version_id, path (denorm), rep_type
(hypothetical_q|proposition|summary|alt_phrasing|synonym_expansion|table_desc|figure_desc|keyword_set),
rep_text, rep_tsv (generated), rep_embedding, gen_model.`
Indexes: `hnsw(rep_embedding)`, `gin(rep_tsv)`, `gist(path)`.

**The four required properties, physically:**
- **Semantically configured** → `content_embedding` per node (+ every `arep.rep_embedding` is an
  extra semantic surface). Late chunking optional (model-dependent).
- **Logically linked** → `parent_id` + `path` (ltree) tree edges, plus `cross_refs[]` (DAG) and
  `entity_ids[]` (entity graph).
- **Contextually enriched** → `context_prefix` per node (situates it in the whole document;
  prepended before embedding and folded into FTS). This is the technique `retrieval/contextual.py`
  already implements.
- **Accessibility enhancement** → the whole `arep` table: hypothetical questions, atomic
  propositions (Dense-X), summaries, alt-phrasings/synonyms, table/figure prose descriptions —
  each independently embedded + FTS-indexed so diverse downstream queries all land.

**Serving the three consumer needs (all on the same rows):**
- *Keyword search* → hybrid `content_tsv` + `rep_tsv` (GIN) fused with vector legs via RRF.
- *Single-document Q&A* → filter `doc_id`, hybrid over `arep` (hypothetical_q + proposition hit
  hardest) → map aids back to `knode` → walk up to parent section → rerank → assemble.
- *"All data about a PART of a document"* → pure `path <@ :section_path` subtree query (GiST),
  and the same predicate cheaply scopes any vector/keyword search to that part.

**`fact` nodes are first-class returnable knodes** (both LLM-extracted attribute KVs and
deterministic checksum-validated fields), so downstream consumers get a structured fact layer,
not just retrieval hits.

### 3.4 Versioning (re-upload of a similar document)

Immutable copy-on-write. `doc_version(id, doc_id, version_no, content_hash (dedup guard),
supersedes, is_current (partial-unique per doc_id), created_at/by, changed_fields jsonb)`.
On re-upload: hash → if identical, no-op; else new version (path carries `.v<n>`), structural
diff old vs new, **reuse embeddings/aids for unchanged nodes** (cost control), flip `is_current`.
Default reads filter `is_current = TRUE`; time-travel reads pass an explicit `version_id`.
v1 simplification: full per-version node copies (simpler) unless storage-bound.

### 3.5 Dual extraction

- **LLM path (Stellar):** base classification + LLM-chosen attribute KV facts → `fact` knodes.
- **Deterministic path (no cloud):** strict per-doc-type pydantic schemas from the OCR dump.
  - Self-validating IDs (checksum) captured by **global regex sweep + checksum**, label-free-safe.
    **North America scope (India dropped):** Passport MRZ (ICAO 9303 mod-10, universal),
    US SSN/EIN/ITIN (structural), Canada SIN/BN (Luhn), per-state/province driver-licence
    patterns. **If Mexico is in scope (Q-J):** add CURP (18-char, check digit), RFC
    (12/13-char), INE/IFE voter card — requires a follow-up research pass.
  - Non-checksum fields → bbox-geometry label-anchored KV (rapidfuzz label match + nearest
    right/below value binding + dateparser/usaddress/libpostal validators).
  - Library: `python-stdnum`, `PassportEye`/`fastmrz`, `dateparser`, `usaddress`/libpostal,
    `rapidfuzz`, `pydantic`, `pycountry`. Every field emitted as
    `{value, raw_ocr, source, checksum_ok, confidence, bbox}`.

### 3.6 Local classifier + PII gate stack

`scikit-learn` (TF-IDF 1-2gram + LinearSVC calibrated) primary; `setfit`+`sentence-transformers`
few-shot upgrade; rules/anchors as Stage-0 + Snorkel weak-supervision label bootstrap;
`presidio-analyzer`/`-anonymizer` + spaCy for PII detection/scoring/redaction. All sync libs run
via `run_in_executor`; per-doc decision trace persisted to Postgres for audit.

### 3.7 Embeddings

Default to the existing provider-agnostic surface (`get_stellar()` → Stellar `gte-large-en-v1.5`
**1024-D**, or Vertex 768-D), runtime dim discovery + HNSW `vector_cosine_ops`, exactly like
`retrieval`. Consider `halfvec` storage given multi-vector amplification (~5–15× rows). Late
chunking is an optional enhancement only if the embedding model supports long-context mean pooling.

---

### 3.8 North-America scope specifics (geography + multilingual gate)

**Jurisdictions:** US, Canada, Mexico. **Languages:** English + Spanish; bilingual docs supported.
French / Indigenous languages → detect-and-defer (route to `DETERMINISTIC_ONLY`, regex ID
recognizers still fire).

**Language stage (new, between OCR and Stage 0):** `lingua-py` (constrained to EN/ES) gives the
dominant language + per-span languages for bilingual dumps → a `lang_profile` persisted to the
decision trace; tiny/low-confidence/out-of-scope spans fail safe.

**Classifier (Stage 1):** single `LinearSVC` over a `FeatureUnion` of char_wb(3–5) + word(1–2)
TF-IDF on a bilingual corpus (char n-grams = OCR-robust + partly cross-lingual), calibrated for
probabilities; SetFit upgrade uses `paraphrase-multilingual-MiniLM-L12-v2`. Spanish anchor
gazetteer added to Stage 0 (CURP, RFC, INE/IFE, clave de elector, comprobante de domicilio,
constancia de situación fiscal, acta constitutiva).

**PII (Stage 2):** ONE multilingual Presidio `AnalyzerEngine` (`en_core_web_lg` + `es_core_news_lg`
— NOT `es_dep_news_trf`, which has no NER), `supported_languages=['en','es']`, iterate language
spans. Custom MX `PatternRecognizer`s: CURP (`stdnum.mx.curp` validator), RFC (`stdnum.mx.rfc`),
INE Clave de Elector (regex + Spanish context, no checksum → low base score). MX national IDs map
to the same CRITICAL tier as US SSN / Canada SIN.

**Deterministic ID schemas by jurisdiction (no-LLM path):**
- *Universal:* Passport ICAO 9303 MRZ (PassportEye/`mrz`, weighted 7-3-1 mod-10).
- *US:* SSN/EIN/ITIN (`stdnum.us.*`, structural), per-state DL patterns, W-2/1099 box anchors.
- *Canada:* SIN/BN (Luhn, `stdnum.ca.*`), per-province DL, T4/NOA anchors.
- *Mexico:* CURP (18-char, **hard** check digit via `stdnum.mx.curp` + 32-state catalog + embedded
  DOB/sex/entity cross-checks; accept sex code `X`), RFC (12/13-char, structure strict, mod-11
  check **SOFT** — ~1.5% of legit RFCs fail), INE/IFE (Clave de Elector + reverse TD1 MRZ CIC/OCR;
  branch on model D+ for CIC/QR; **no public checksum** → cross-field reconciliation only), SAT CSF
  (idCIF + QR → the only doc with a free gov verification endpoint), acta de nacimiento,
  comprobante de domicilio (**≤3-month recency gate**).

**Persona-moral (MX corporate) nuance:** company onboarding is *recursive* — legal rep + each
≥25% beneficial owner implies a persona-física sub-expediente. **v1 boundary:** we recognize the
corporate doc types (acta constitutiva, poder notarial, CSF) and **capture + link** ownership
facts in the merged view, but do **not** auto-walk/adjudicate the beneficial-owner graph (that's
the deferred AML scope, D2).

**Caveats baked in:** RFC checksum = WARN not REJECT; CURP accepts sex `X`; INE numbers are
format/consistency-validated only (not checksum-proven); required-document lists are
**config-driven** (regulatory drift, e.g. 2026 CNBV changes).

### 3.9 Knowledge-subtree headline capabilities (the differentiators — "the star")

1. **Answer at any altitude** — collapsed-tree retrieval spans fact → chunk → section → doc
   summary; query auto-selects granularity. [v1]
2. **Self-describing & introspectable** — per-subtree **capabilities manifest** (node types,
   attributes, languages, verification status) + **answerable-questions index** (from
   hypothetical_q `arep`). Lets non-agentic downstream traverse cold. [v1]
3. **Verifiable by construction** — every fact: provenance (page+bbox) + **verification_status**
   (`checksum_verified` / `gov_verified` / `llm_unverified`) + confidence; "verified only" queries. [v1]
4. **Cross-lingual by default** — Spanish↔English normalized `arep` so a query in one language
   hits facts in the other; no query-time translation. [v1, given EN/ES scope]
5. **Access-aware projections** — per-node PII sensitivity → same subtree served full or
   redacted/masked by caller clearance. [**v1 — but TOGGLEABLE (default-off-able) and
   non-breaking**: when masking is on, only sensitive spans/values are masked; structure,
   non-PII content, provenance and traversal stay fully intact; when off, full view. (D13)]
6. **Time-travel & change-awareness** — version chain + validity intervals → "as of date X" +
   "what changed since last upload" delta feed for periodic re-KYC. [v1]
7. **Open representation system** — `arep` is open; new aids/projections are rows, not migrations. [inherent]
8. **Hybrid retrieval at any scope** — dense + lexical + structural(ltree path) via RRF + rerank,
   scoped to client/doc/section. [v1]

### 3.10 Retrieval-service reuse (the existing `retrieval` module, URLs via env var)

The running `retrieval` service exposes: `POST /api/retrieve` (hybrid dense+sparse+RRF+rerank+MMR
+CRAG+contextual → ranked ChunkHits), `POST /api/ingest/wega` (WegaChunker chunk+embed+store),
`POST /api/ingest/contextual`, `/api/documents/*`, `/api/kyc/*`. **Two gaps:** (a) `/api/retrieve`
has **no per-client/doc filter** (whole-corpus only); (b) there is **no bare `/embed` endpoint**
(embedding is internal to ingest/retrieve). Our chunking is **structure-aware** (tied to `knode`s),
unlike WegaChunker's generic token chunks — so structural chunking stays ours; only embedding +
generic search are reuse candidates.

**RESOLVED (D12):** rather than choose among the partial-reuse options, we specify the endpoints
retrieval should ADD so it becomes the single model gateway — see
[retrieval-api-requirements.md](retrieval-api-requirements.md): `POST /api/embed` (required),
`POST /api/llm/complete` (required), `POST /api/rerank` + `GET /api/models` (recommended),
optional `/api/contextual-prefix` and a `filter` on `/api/retrieve`. document_intelligence then
needs **no Stellar/COIN/SSL wiring of its own** — it calls retrieval for all embeddings + LLM +
rerank, and stores its own per-client `knode`/`arep` vectors (scoped multi-vector search stays
local). Another agent will implement these in the `retrieval` repo and deploy.

### 3.11 Classifier training posture

**No training required to launch.** Stage-0 anchors + ID regex + **checksums** classify
fixed-format docs out of the box; Presidio PII uses pretrained spaCy NER + rules; zero-shot covers
the rest as a stopgap. For messy/variable docs, **weak supervision** (anchor rules → Snorkel
labeling functions → auto-labels on unlabeled OCR dumps → TF-IDF+SVM) needs **no hand-labeled
corpus**; SetFit needs ~8–16 examples/hard class; hand-label only a small gold eval set. Targeted
supervised training is added only where rules can't separate two doc types, as samples accrue.
Need: a sample corpus of real docs per type (Q-M).

## 4. Open questions / to confirm

- **Q-A (accessibility enhancement)** — RESOLVED: = retrieval aids in `arep`; **full set** (D8).
- **Q-B (cross-document)** — RESOLVED: **full cross-document merge** (D7) → client-level merged view.
- **Q-C (versioning)** — full per-version node copies (v1 default) vs shared immutable nodes. [default, confirm]
- **Q-D (aids set + timing)** — RESOLVED: full set (D8). Timing: async backfill after ingest. [default, confirm]
- **Q-E (deterministic priority)** — REVISED for NA: passport (MRZ), SSN, EIN, ITIN, SIN, BN,
  state/province DLs (+ CURP/RFC if Mexico). [proposed, confirm]
- **Q-F (scale)** — RESOLVED: millions of clients → HASH-partition by `client_id` (D11).
- **Q-G (languages)** — RESOLVED: **English + Spanish**, North America; India out (D9, D10).
- **Q-H (embeddings)** — keep existing Stellar 1024-D (no late chunking) by default. [default, confirm]
- **Q-I (redact-then-send)** — keep `REDACT_THEN_SEND` as a designed (inactive) gate branch. [proposed]
- **Q-J (Mexico in scope?)** — RESOLVED: **yes, US + Canada + Mexico**. Mexican KYC docs
  (CURP/RFC/INE/comprobante de domicilio) + Spanish NLP in scope; research pass running.
- **Q-K (merge conflict policy)** — RESOLVED: **confidence-weighted + flag** (see §3.1).
- **Q-L (retrieval reuse boundary)** — RESOLVED (D12): retrieval becomes the model gateway via
  new endpoints (see [retrieval-api-requirements.md](retrieval-api-requirements.md)); our scoped
  vector index stays local.
- **Q-M (sample corpus)** — can the owner provide a sample set (even unlabeled) of real documents
  per type to bootstrap weak-supervision training + eval? [pending — non-blocking]
- **Q-N (knowledge-subtree v1 capability set)** — RESOLVED (D13): **all** §3.9 capabilities in v1,
  with masking/redaction toggleable + non-breaking.

## 5. Out of v1 scope (YAGNI)

Entity resolution / name matching; risk scoring; PEP/sanctions adjudication; cross-client AML
linking; graph DB; human-review UI (flags + list endpoint only); full bitemporal audit; local
Azure DI container.

---

## Changelog

- **2026-06-24 (1)** — Initial record: vision, decisions D1–D6, the per-document knowledge-subtree
  direction, versioning, PII-aware gate, dual extraction, India/US/Canada taxonomy requirement.
- **2026-06-24 (2)** — Synthesized architecture from 4 research threads: unified path-encoded
  forest (§3.1), full ingestion+gate pipeline (§3.2), the `knode`+`arep` two-table knowledge
  subtree realizing all four properties (§3.3), copy-on-write versioning (§3.4), dual extraction
  with checksum-validated deterministic schemas (§3.5), local classifier + Presidio gate stack
  (§3.6), embedding strategy (§3.7). Updated open questions to Q-A…Q-I.
- **2026-06-24 (3)** — Owner answers round 4: D7 full cross-document merge (added client-level
  merged knowledge view §3.1 with intra-client fact consolidation), D8 full accessibility-rep
  set, D9 geography = North America (India dropped), D10 English + Spanish, D11 millions of
  clients → HASH partition by client_id. Resolved Q-A/Q-B/Q-D/Q-F/Q-G; revised Q-E to NA docs;
  added Q-J (Mexico in scope?) and Q-K (merge conflict policy).
- **2026-06-24 (4)** — Owner answers round 5: Q-J = US + Canada + Mexico (Mexican docs + Spanish
  in scope); Q-K = confidence-weighted + flag. Launched focused research on Mexican KYC document
  taxonomy/ID-format schemas + the Spanish-language classifier/PII NLP stack.
- **2026-06-24 (5)** — Folded Mexico + Spanish research into the design as §3.8: jurisdiction
  scope (US/Canada/Mexico), bilingual language stage (lingua) + multilingual Presidio with custom
  CURP/RFC/INE recognizers, per-jurisdiction deterministic ID schemas (incl. CURP hard / RFC soft
  checksums, INE reverse MRZ, SAT CSF gov-verify), and the persona-moral v1 boundary
  (capture+link, no beneficial-owner adjudication).
- **2026-06-24 (6)** — Owner round 6: reuse the running `retrieval` service via env-var URLs for
  embedding + generic retrieve (inspected its API surface → §3.10; two gaps surfaced: no
  per-client filter on `/api/retrieve`, no bare `/embed`). Clarified classifier-training posture
  (§3.11: no training to launch; rules + weak supervision; targeted training later). Brainstormed
  and recorded the knowledge-subtree headline capabilities (§3.9). Added Q-L (retrieval boundary),
  Q-M (sample corpus), Q-N (v1 capability set).
- **2026-06-24 (7)** — Owner round 7: D12 (route all model access — embed/LLM/rerank — through the
  `retrieval` service; wrote [retrieval-api-requirements.md](retrieval-api-requirements.md) for the
  other agent; document_intelligence keeps its own per-client scoped vector index, no COIN/VDI
  creds) and D13 (ALL §3.9 capabilities in v1 incl. toggleable, non-breaking masking/redaction).
  Resolved Q-L and Q-N.
