"""Live-DB round-trip for the store/repository layer.

Marked ``db``. Skips cleanly when Postgres is unreachable or the configured credentials don't
connect (e.g. local default user). Runs in CI with PG_* env pointed at a database that has the
``ltree`` extension. pgvector is optional — the FTS legs of hybrid search are exercised regardless.
"""
from __future__ import annotations

import uuid

import pytest

from di.models import (
    ClientFact,
    DocumentMeta,
    KNode,
    NodeType,
    Provenance,
    SensitivityBucket,
    VerificationStatus,
)

pytestmark = pytest.mark.db


@pytest.mark.asyncio
async def test_store_round_trip():
    from di import store
    from di.db import close_pool, init_pool, run_migrations

    try:
        await init_pool()
        await run_migrations()
    except Exception as e:  # noqa: BLE001 - any connect/auth/DDL failure -> skip, not fail
        pytest.skip(f"Postgres unavailable/unauthorized: {e}")

    try:
        cid = f"test-{uuid.uuid4().hex[:8]}"
        base = f"client_{cid.replace('-', '_')}.doctype_passport.v1"
        meta = DocumentMeta(
            client_id=cid, document_name="passport.pdf", doc_type="PASSPORT",
            sensitivity_bucket=SensitivityBucket.critical, page_count=1,
        )
        doc_id = await store.insert_document(meta, ocr_text="PASSPORT Jane Doe nationality EXAMPLE")
        ver = await store.create_version(
            cid, doc_id, content_hash="h1", version_no=1, supersedes_id=None)

        root = KNode(id=str(uuid.uuid4()), client_id=cid, doc_id=doc_id, version_id=ver,
                     path=base, node_type=NodeType.document, depth=3, title="PASSPORT")
        chunk = KNode(id=str(uuid.uuid4()), client_id=cid, doc_id=doc_id, version_id=ver,
                      parent_id=root.id, path=f"{base}.s0.c0", node_type=NodeType.chunk, depth=5,
                      content="Jane Doe, nationality EXAMPLE, passport number X1234567",
                      provenance=Provenance(page=1))
        fact = KNode(id=str(uuid.uuid4()), client_id=cid, doc_id=doc_id, version_id=ver,
                     parent_id=root.id, path=f"{base}.f0", node_type=NodeType.fact, depth=4,
                     attribute_key="id.passport_number", value_text="X1234567",
                     verification_status=VerificationStatus.checksum_verified, confidence=0.95,
                     sensitivity=SensitivityBucket.critical)
        await store.insert_knodes([root, chunk, fact])

        # full subtree
        sub = await store.fetch_subtree(cid, doc_id=doc_id)
        types_ = {n["node_type"] for n in sub}
        assert {"document", "chunk", "fact"} <= types_
        assert len(sub) == 3

        # ltree-scoped subtree (only the chunk branch)
        scoped = await store.fetch_subtree(cid, path_prefix=f"{base}.s0")
        assert all(n["path"].startswith(f"{base}.s0") for n in scoped)
        assert any(n["node_type"] == "chunk" for n in scoped)

        # hybrid search (FTS leg always; vector leg only if pgvector present)
        hits = await store.hybrid_search(cid, query_text="passport number nationality", top_k=5)
        assert hits, "expected at least the chunk to match"
        assert hits[0]["_rank"] == 1

        # merged fact upsert (idempotent on conflict)
        cf = ClientFact(client_id=cid, attribute_key="id.passport_number",
                        resolved_value="X1234567", confidence=0.95, source_fact_ids=[fact.id])
        await store.upsert_merged_facts([cf])
        await store.upsert_merged_facts([cf])  # second time must not raise

        docs = await store.list_documents(cid)
        assert any(d["document_name"] == "passport.pdf" for d in docs)
    finally:
        await close_pool()
