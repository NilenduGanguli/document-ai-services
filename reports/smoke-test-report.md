# Smoke test report

- Target: `http://localhost:8090`
- Client: `SMOKE-CLIENT-001`
- Result: **49/49 passed**

| Check | Result | Detail |
|---|---|---|
| /readyz reports ready | PASS | degraded=[] |
|   component db | PASS | connected {'host': 'db', 'database': 'document_intelligence'} |
|   component migrations | PASS | applied (mode=auto) {} |
|   component posture | PASS | non-production: guards inactive {} |
|   component rls | PASS | tenant_isolation verified on every tenant table; runtime role is least-privilege {} |
|   component pgvector | PASS | available {'enabled': True} |
|   component retrieval | PASS | in-process stub {'stub': True} |
|   component blob | PASS | table "di".di_blob {'backend': 'postgres'} |
|   component ocr | PASS | azure read v3.2 {'endpoint': 'http://azure-ocr-mock:5000', 'configured': True} |
|   component auth | PASS | enabled {'bootstrap_seeded': True} |
| /health is liveness-only 200 | PASS |  |
| /metrics exposes prometheus text | PASS | 3480 bytes |
| no API key -> 401 | PASS | got 401 |
| bad API key -> 401 | PASS | got 401 |
| valid API key -> 200 | PASS | got 200 |
| POST /ingest -> 202 + job_id | PASS | got 202 {"job_id":"dbf9fb2c-3126-41bb-beb7-e4c274cae54a","client_id":"SMOKE-CLIENT-001","status":"queued","document_name":"passp |
| job reached succeeded | PASS | status=succeeded error=None |
|   stage recorded: ocr | PASS |  |
|   stage recorded: gate | PASS |  |
|   stage recorded: extract | PASS |  |
|   stage recorded: subtree | PASS |  |
|   stage recorded: merge | PASS |  |
|   stage recorded: done | PASS |  |
| job carries doc_id | PASS | 347efe81-62a2-4570-a98b-f75c22b238ff |
| blob retained via configured backend | PASS | backend=postgres |
| same idempotency_key reuses the job | PASS | job_id=dbf9fb2c-3126-41bb-beb7-e4c274cae54a |
| identical re-upload no-ops (hash before OCR) | PASS | stages=['version', 'done'] |
| GET /documents | PASS | count=1 |
|   document list omits raw OCR text | PASS |  |
|   external_document_id round-tripped | PASS | EXT-PASSPORT-1 |
|   classified doc_type | PASS | PASSPORT |
| GET /tree | PASS | nodes=11 |
|   masked by default (server policy) | PASS |  |
| GET /facts | PASS | count=7 |
|   sensitive values redacted by default | PASS | 6 sensitive facts |
|   mask=false returns cleartext | PASS |  |
| verified_only excludes self-scored LLM facts | PASS | 0 verified facts |
| POST /search | PASS | hits=5 vector=True |
| top_k above the cap is rejected | PASS | got 422 |
| GET /nodes/{id}/provenance | PASS | extractor=None |
| GET /changes | PASS | count=1 |
|   change feed exposes a monotonic cursor | PASS | next_seq=11 |
|   after_seq cursor drains to empty | PASS |  |
| GET /manifest | PASS |  |
| GET /answerable | PASS |  |
| GET /jobs paginates | PASS | next_cursor=set |
| POST /admin adjudicate | PASS | {"client_id":"SMOKE-CLIENT-001","attribute_key":"identity.family_name","verdict":"override","remerge |
| POST /admin purge (right-to-erasure) | PASS | {'arep': 0, 'knode': 11, 'doc_version': 1, 'client_merged_fact': 7, 'di_fact_adjudication': 1, 'di_decision_trace': 1, ' |
|   tenant data is gone | PASS |  |
