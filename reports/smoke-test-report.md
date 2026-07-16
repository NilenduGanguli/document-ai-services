# Smoke test report

- Target: `http://localhost:8090`
- Client: `SMOKE-CLIENT-001`
- Result: **60/60 passed**

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
|   component audit | PASS | writer started; partition horizon 4 month(s) {'horizon_months': 4, 'strict': False} |
| /health is liveness-only 200 | PASS |  |
| /metrics exposes prometheus text | PASS | 6990 bytes |
| no API key -> 401 | PASS | got 401 |
| bad API key -> 401 | PASS | got 401 |
| valid API key -> 200 | PASS | got 200 |
| POST /ingest -> 202 + job_id | PASS | got 202 {"job_id":"bff75cb4-9927-415d-9d91-7d73f06d715c","client_id":"SMOKE-CLIENT-001","status":"succeeded","document_name":"pa |
| job reached succeeded | PASS | status=succeeded error=None |
|   stage recorded: ocr | PASS |  |
|   stage recorded: gate | PASS |  |
|   stage recorded: extract | PASS |  |
|   stage recorded: subtree | PASS |  |
|   stage recorded: merge | PASS |  |
|   stage recorded: done | PASS |  |
| job carries doc_id | PASS | 9516fade-e1b7-4dc7-a2c3-eac1e567e6bf |
| blob retained via configured backend | PASS | backend=postgres |
| same idempotency_key reuses the job | PASS | job_id=bff75cb4-9927-415d-9d91-7d73f06d715c |
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
|   change feed exposes a monotonic cursor | PASS | next_seq=13 |
|   after_seq cursor drains to empty | PASS |  |
| GET /manifest | PASS |  |
| GET /answerable | PASS |  |
| GET /jobs paginates | PASS | next_cursor=set |
| POST /admin/keys creates a key | PASS | got 201 |
| POST /admin/keys/{id}/rotate mints a successor | PASS | got 200 {'key_id': '1434e759-af8e-486a-8c2e-aa78d9cbee6d', 'api_key': 'di_2oK9yvp51rfIyIJ-fx3xpfzBre0zb-Q5uSeQxIcQe_Y', 'name': 'smoke-rotate@20260716', 'old_key_expires_at': '2026-07-17T07:24:38.618230'} |
|   rotated (successor) key authenticates | PASS | got 200 |
|   successor key lists with rotated_from set | PASS | {'id': '1434e759-af8e-486a-8c2e-aa78d9cbee6d', 'name': 'smoke-rotate@20260716', 'client_ids': ['SMOKE-CLIENT-001'], 'scopes': ['read'], 'created_at': '2026-07-16T07:24:38.617970Z', 'last_used_at': '2026-07-16T07:24:38.620985Z', 'disabled_at': None, 'expires_at': None, 'rotated_from': 'c3ecc134-819b-4632-8d84-082eb8482e1d', 'rate_limit_rps': None, 'created_by': None} |
| PUT /admin/tenants/{id}/policy sets an override | PASS | got 200 {"client_id":"SMOKE-CLIENT-001-QUOTA","max_active_jobs":null,"daily_ingest_limit":0,"note":null,"updated_at":"2026-07-16 |
|   blocked tenant's ingest -> 429 | PASS | got 429 |
|   clearing the policy override succeeds | PASS | got 200 |
| GET /admin/access-log | PASS | count=5 |
| POST /admin adjudicate | PASS | {"client_id":"SMOKE-CLIENT-001","attribute_key":"identity.family_name","verdict":"override","remerge |
| POST /admin purge (right-to-erasure) | PASS | {'arep': 0, 'knode': 11, 'doc_version': 1, 'client_merged_fact': 7, 'di_fact_adjudication': 1, 'di_decision_trace': 1, ' |
|   tenant data is gone | PASS |  |
| concurrent burst trips the rate limiter | PASS | 114/200 got 429 |
|   429 carries Retry-After | PASS | {'date': 'Thu, 16 Jul 2026 07:24:37 GMT', 'server': 'uvicorn', 'retry-after': '1', 'content-length': '49', 'content-type': 'application/json'} |
