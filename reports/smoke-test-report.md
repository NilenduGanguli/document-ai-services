# Smoke test report

- Target: `http://localhost:8090`
- Client: `DEMO-CLIENT`
- Result: **45/45 passed**

| Check | Result | Detail |
|---|---|---|
| /readyz reports ready | PASS | degraded=[] |
|   component db | PASS | connected {'host': 'db', 'database': 'document_intelligence'} |
|   component migrations | PASS | applied {} |
|   component pgvector | PASS | available {'enabled': True} |
|   component retrieval | PASS | in-process stub {'stub': True} |
|   component blob | PASS | bucket=document-intelligence endpoint=http://minio:9000 {'backend': 's3'} |
|   component ocr | PASS | azure read v3.2 {'endpoint': 'http://azure-ocr-mock:5000', 'configured': True} |
|   component auth | PASS | enabled {'bootstrap_seeded': True} |
| /health is liveness-only 200 | PASS |  |
| /metrics exposes prometheus text | PASS | 3466 bytes |
| no API key -> 401 | PASS | got 401 |
| bad API key -> 401 | PASS | got 401 |
| valid API key -> 200 | PASS | got 200 |
| POST /ingest -> 202 + job_id | PASS | got 202 {"job_id":"150df1d9-0ac3-4c36-a442-3b37b939927c","client_id":"DEMO-CLIENT","status":"queued","document_name":"passport.p |
| job reached succeeded | PASS | status=succeeded error=None |
|   stage recorded: ocr | PASS |  |
|   stage recorded: gate | PASS |  |
|   stage recorded: extract | PASS |  |
|   stage recorded: subtree | PASS |  |
|   stage recorded: merge | PASS |  |
|   stage recorded: done | PASS |  |
| job carries doc_id | PASS | 46104fab-59bf-48f5-82bb-30647797f80e |
| blob retained via configured backend | PASS | backend=s3 |
| same idempotency_key reuses the job | PASS | job_id=150df1d9-0ac3-4c36-a442-3b37b939927c |
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
|   change feed exposes a monotonic cursor | PASS | next_seq=8 |
|   after_seq cursor drains to empty | PASS |  |
| GET /manifest | PASS |  |
| GET /answerable | PASS |  |
| GET /jobs paginates | PASS | next_cursor=set |
| POST /admin adjudicate | PASS | {"client_id":"DEMO-CLIENT","attribute_key":"identity.family_name","verdict":"override","remerged_fac |
