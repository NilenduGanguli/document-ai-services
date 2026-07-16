# Smoke test report

- Target: `http://localhost:8090`
- Client: `SMOKE-CLIENT-001`
- Result: **63/63 passed**

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
| /metrics exposes prometheus text | PASS | 3483 bytes |
| no API key -> 401 | PASS | got 401 |
| bad API key -> 401 | PASS | got 401 |
| valid API key -> 200 | PASS | got 200 |
| POST /ingest -> 202 + job_id | PASS | got 202 {"job_id":"bf57350a-fe27-4b81-98fb-be3585def880","client_id":"SMOKE-CLIENT-001","status":"queued","document_name":"passp |
| job reached succeeded | PASS | status=succeeded error=None |
|   stage recorded: ocr | PASS |  |
|   stage recorded: gate | PASS |  |
|   stage recorded: extract | PASS |  |
|   stage recorded: subtree | PASS |  |
|   stage recorded: merge | PASS |  |
|   stage recorded: done | PASS |  |
| job carries doc_id | PASS | 46cef4c9-a847-4a88-aa72-60fed1952309 |
| blob retained via configured backend | PASS | backend=postgres |
| same idempotency_key reuses the job | PASS | job_id=bf57350a-fe27-4b81-98fb-be3585def880 |
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
|   change feed exposes a monotonic cursor | PASS | next_seq=38 |
|   after_seq cursor drains to empty | PASS |  |
| GET /manifest | PASS |  |
| GET /answerable | PASS |  |
| GET /jobs paginates | PASS | next_cursor=set |
| POST /admin/keys creates a key | PASS | got 201 |
| POST /admin/keys/{id}/rotate mints a successor | PASS | got 200 {'key_id': '02e132ec-82f8-4d00-b928-dedb9484782e', 'api_key': 'di_qEBys2ztLqNEnEOSDbhJxuGX9fml8ASZo-A9kbOujPE', 'name': 'smoke-rotate@20260716', 'old_key_expires_at': '2026-07-17T08:12:51.683521'} |
|   rotated (successor) key authenticates | PASS | got 200 |
|   successor key lists with rotated_from set | PASS | {'id': '02e132ec-82f8-4d00-b928-dedb9484782e', 'name': 'smoke-rotate@20260716', 'client_ids': ['SMOKE-CLIENT-001'], 'scopes': ['read'], 'created_at': '2026-07-16T08:12:51.683188Z', 'last_used_at': '2026-07-16T08:12:51.686366Z', 'disabled_at': None, 'expires_at': None, 'rotated_from': '80ef3727-e66b-4ce0-9dfe-38c2235cc847', 'rate_limit_rps': None, 'created_by': None} |
| PUT /admin/tenants/{id}/policy sets an override | PASS | got 200 {"client_id":"SMOKE-CLIENT-001-QUOTA","max_active_jobs":null,"daily_ingest_limit":0,"note":null,"updated_at":"2026-07-16 |
|   blocked tenant's ingest -> 429 | PASS | got 429 |
|   clearing the policy override succeeds | PASS | got 200 |
| GET /admin/access-log | PASS | count=5 |
| POST /admin adjudicate | PASS | {"client_id":"SMOKE-CLIENT-001","attribute_key":"identity.family_name","instance_key":"","verdict":" |
| GET /admin adjudications lists the live verdict | PASS | got 200 |
| GET /admin adjudications/history records the verdict | PASS | got 200 |
| DELETE /admin adjudications clears the verdict | PASS | got 200 {"client_id":"SMOKE-CLIENT-001","attribute_key":"identity.family_name","instance_key":"","cleared":t |
| POST /admin purge (right-to-erasure) | PASS | {'arep': 0, 'knode': 11, 'doc_version': 1, 'client_merged_fact': 7, 'di_fact_adjudication': 0, 'di_decision_trace': 1, ' |
|   tenant data is gone | PASS |  |
| concurrent burst trips the rate limiter | PASS | 108/200 got 429 |
|   429 carries Retry-After | PASS | {'date': 'Thu, 16 Jul 2026 08:12:51 GMT', 'server': 'uvicorn', 'retry-after': '1', 'content-length': '49', 'content-type': 'application/json'} |
