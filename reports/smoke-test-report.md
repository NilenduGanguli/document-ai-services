# Smoke test report

- Target: `http://localhost:8090`
- Client: `SMOKE-CLIENT-P4`
- Result: **70/70 passed**

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
|   component queue | PASS | ready {'depth': 0, 'embedded_worker': False} |
| /health is liveness-only 200 | PASS |  |
| /metrics exposes prometheus text | PASS | 4595 bytes |
|   /metrics exposes queue depth gauge | PASS |  |
|   /metrics exposes jobs-inflight gauge | PASS |  |
| no API key -> 401 | PASS | got 401 |
| bad API key -> 401 | PASS | got 401 |
| valid API key -> 200 | PASS | got 200 |
| POST /ingest -> 202 + job_id | PASS | got 202 {"job_id":"3a8e66f9-65cb-49c1-873d-87c52c29dd77","client_id":"SMOKE-CLIENT-P4","status":"queued","document_name":"passpo |
| job reached succeeded | PASS | status=succeeded error=None |
|   stage recorded: ocr | PASS |  |
|   stage recorded: gate | PASS |  |
|   stage recorded: extract | PASS |  |
|   stage recorded: subtree | PASS |  |
|   stage recorded: merge | PASS |  |
|   stage recorded: done | PASS |  |
| job carries doc_id | PASS | 19685dba-c87c-48ee-bf5d-d9e16a4a5c3c |
| blob retained via configured backend | PASS | backend=postgres |
| same idempotency_key reuses the job | PASS | job_id=3a8e66f9-65cb-49c1-873d-87c52c29dd77 |
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
|   change feed exposes a monotonic cursor | PASS | next_seq=59 |
|   after_seq cursor drains to empty | PASS |  |
| GET /manifest | PASS |  |
| GET /answerable | PASS |  |
| GET /jobs paginates | PASS | next_cursor=set |
| job carries kind=ingest | PASS | kind=ingest |
| job carries attempts/max_attempts | PASS | attempts=1 max_attempts=3 |
|   retry on a non-dead job -> 404 | PASS | got 404 |
|   cancel on a non-queued job -> 404 | PASS | got 404 |
| POST /admin/keys creates a key | PASS | got 201 |
| POST /admin/keys/{id}/rotate mints a successor | PASS | got 200 {'key_id': 'f3d70495-6030-42df-9068-b2b5d7c86fe7', 'api_key': 'di_D8VW5V4YNId8ZNQ9BskqAtZL7BZ_yJSMv8HrwVJvA3c', 'name': 'smoke-rotate@20260716', 'old_key_expires_at': '2026-07-17T13:32:21.237854'} |
|   rotated (successor) key authenticates | PASS | got 200 |
|   successor key lists with rotated_from set | PASS | {'id': 'f3d70495-6030-42df-9068-b2b5d7c86fe7', 'name': 'smoke-rotate@20260716', 'client_ids': ['SMOKE-CLIENT-P4'], 'scopes': ['read'], 'created_at': '2026-07-16T13:32:21.237610Z', 'last_used_at': '2026-07-16T13:32:21.240687Z', 'disabled_at': None, 'expires_at': None, 'rotated_from': 'd2233312-df54-49e2-83b5-6ea913af1eed', 'rate_limit_rps': None, 'created_by': None} |
| PUT /admin/tenants/{id}/policy sets an override | PASS | got 200 {"client_id":"SMOKE-CLIENT-P4-QUOTA","max_active_jobs":null,"daily_ingest_limit":0,"note":null,"updated_at":"2026-07-16T |
|   blocked tenant's ingest -> 429 | PASS | got 429 |
|   clearing the policy override succeeds | PASS | got 200 |
| GET /admin/access-log | PASS | count=5 |
| POST /admin adjudicate | PASS | {"client_id":"SMOKE-CLIENT-P4","attribute_key":"identity.family_name","instance_key":"","verdict":"o |
| GET /admin adjudications lists the live verdict | PASS | got 200 |
| GET /admin adjudications/history records the verdict | PASS | got 200 |
| DELETE /admin adjudications clears the verdict | PASS | got 200 {"client_id":"SMOKE-CLIENT-P4","attribute_key":"identity.family_name","instance_key":"","cleared":tr |
| POST /admin purge (right-to-erasure) | PASS | {'arep': 0, 'knode': 11, 'doc_version': 1, 'client_merged_fact': 7, 'di_fact_adjudication': 0, 'di_decision_trace': 1, ' |
|   tenant data is gone | PASS |  |
| concurrent burst trips the rate limiter | PASS | 112/200 got 429 |
|   429 carries Retry-After | PASS | {'date': 'Thu, 16 Jul 2026 13:32:21 GMT', 'server': 'uvicorn', 'retry-after': '1', 'content-length': '49', 'content-type': 'application/json'} |
