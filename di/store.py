"""Persistence / repository layer for the knowledge tree.

All access goes through ``di.db.acquire(client_id)`` so the RLS tenant GUC is bound for the
checkout. Vector search degrades gracefully to full-text search when pgvector is absent. The
hybrid search implements the "index-many / return-parent" pattern: it searches both ``knode``
content and ``arep`` representations, maps ``arep`` hits back to their parent ``knode``, and
fuses the legs with Reciprocal Rank Fusion.
"""
from __future__ import annotations

import uuid
from typing import Any

from di.config import get_settings
from di.db import acquire, embedding_dim, pgvector_available, vec_to_pg
from di.models import ARep, ClientFact, DocumentMeta, GateResult, KNode

# Column order for knode inserts WITHOUT the runtime embedding column.
_KNODE_COLS = (
    "id", "client_id", "doc_id", "version_id", "parent_id", "path", "node_type", "seq", "depth",
    "title", "content", "context_prefix", "attribute_key", "value_text", "value_date", "value_num",
    "verification_status", "confidence", "sensitivity", "valid_from", "valid_to",
    "cross_refs", "entity_ids", "provenance", "token_count",
)
_AREP_COLS = (
    "id", "knode_id", "client_id", "doc_id", "version_id", "path",
    "rep_type", "rep_lang", "rep_text", "gen_model",
)


def _schema() -> str:
    return get_settings().pg_schema


def _new_id(existing: str | None) -> str:
    return existing or str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Documents & versions
# ---------------------------------------------------------------------------
async def find_document(client_id: str, document_name: str) -> dict[str, Any] | None:
    s = _schema()
    async with acquire(client_id) as conn:
        row = await conn.fetchrow(
            f'SELECT * FROM "{s}".di_documents '
            "WHERE client_id = $1 AND document_name = $2 AND deleted_at IS NULL",
            client_id, document_name,
        )
        return dict(row) if row else None


async def insert_document(meta: DocumentMeta, *, ocr_text: str | None = None,
                          ocr_lines: list[dict] | None = None,
                          lang_profile: dict | None = None) -> str:
    """Insert (or UPSERT by client_id+document_name) a document row; returns its id."""
    s = _schema()
    doc_id = _new_id(meta.id)
    async with acquire(meta.client_id) as conn:
        row = await conn.fetchrow(
            f'INSERT INTO "{s}".di_documents '
            "(id, client_id, document_name, s3_uri, sha256, mime, doc_type, doc_category, subject, "
            " jurisdiction, lang_profile, sensitivity_bucket, gate_decision, confidence, ocr_engine, "
            " page_count, ocr_text, ocr_lines) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18) "
            "ON CONFLICT (client_id, document_name) DO UPDATE SET "
            " s3_uri=EXCLUDED.s3_uri, sha256=EXCLUDED.sha256, mime=EXCLUDED.mime, "
            " doc_type=EXCLUDED.doc_type, doc_category=EXCLUDED.doc_category, subject=EXCLUDED.subject, "
            " jurisdiction=EXCLUDED.jurisdiction, lang_profile=EXCLUDED.lang_profile, "
            " sensitivity_bucket=EXCLUDED.sensitivity_bucket, gate_decision=EXCLUDED.gate_decision, "
            " confidence=EXCLUDED.confidence, ocr_engine=EXCLUDED.ocr_engine, "
            " page_count=EXCLUDED.page_count, ocr_text=EXCLUDED.ocr_text, ocr_lines=EXCLUDED.ocr_lines, "
            " updated_at=now(), deleted_at=NULL "
            "RETURNING id",
            doc_id, meta.client_id, meta.document_name, meta.s3_uri, meta.sha256, meta.mime,
            meta.doc_type, meta.doc_category, meta.subject, meta.jurisdiction, lang_profile or {},
            meta.sensitivity_bucket.value, meta.gate_decision.value if meta.gate_decision else None,
            meta.confidence, meta.ocr_engine, meta.page_count, ocr_text, ocr_lines or [],
        )
        return str(row["id"])


async def get_current_version(client_id: str, doc_id: str) -> dict[str, Any] | None:
    s = _schema()
    async with acquire(client_id) as conn:
        row = await conn.fetchrow(
            f'SELECT * FROM "{s}".doc_version '
            "WHERE client_id = $1 AND doc_id = $2 AND is_current",
            client_id, doc_id,
        )
        return dict(row) if row else None


async def create_version(client_id: str, doc_id: str, *, content_hash: str, version_no: int,
                         supersedes_id: str | None, changed_fields: list[dict] | None = None,
                         created_by: str | None = None) -> str:
    """Insert a new version row and flip is_current (one current per doc)."""
    s = _schema()
    version_id = str(uuid.uuid4())
    async with acquire(client_id) as conn:
        async with conn.transaction():
            await conn.execute(
                f'UPDATE "{s}".doc_version SET is_current = false '
                "WHERE client_id = $1 AND doc_id = $2 AND is_current",
                client_id, doc_id,
            )
            await conn.execute(
                f'INSERT INTO "{s}".doc_version '
                "(id, client_id, doc_id, version_no, content_hash, supersedes, is_current, "
                " changed_fields, created_by) "
                "VALUES ($1,$2,$3,$4,$5,$6,true,$7,$8)",
                version_id, client_id, doc_id, version_no, content_hash, supersedes_id,
                changed_fields or [], created_by,
            )
    return version_id


# ---------------------------------------------------------------------------
# Knodes & areps
# ---------------------------------------------------------------------------
def _knode_row(n: KNode, with_embedding: bool) -> tuple:
    base = (
        _new_id(n.id), n.client_id, n.doc_id, n.version_id, n.parent_id, n.path, n.node_type.value,
        n.seq, n.depth, n.title, n.content, n.context_prefix, n.attribute_key, n.value_text,
        n.value_date, n.value_num, n.verification_status.value, n.confidence, n.sensitivity.value,
        n.valid_from, n.valid_to, n.cross_refs, n.entity_ids,
        n.provenance.model_dump(mode="json") if n.provenance else {}, n.token_count,
    )
    if with_embedding:
        return (*base, vec_to_pg(n.embedding) if n.embedding else None)
    return base


async def insert_knodes(nodes: list[KNode]) -> None:
    if not nodes:
        return
    s = _schema()
    with_emb = await pgvector_available()
    cols = list(_KNODE_COLS) + (["content_embedding"] if with_emb else [])
    # path -> ::ltree, content_embedding -> ::vector
    ph = []
    for i, c in enumerate(cols, start=1):
        if c == "path":
            ph.append(f"${i}::ltree")
        elif c == "content_embedding":
            ph.append(f"${i}::vector")
        else:
            ph.append(f"${i}")
    sql = f'INSERT INTO "{s}".knode ({", ".join(cols)}) VALUES ({", ".join(ph)})'
    rows = [_knode_row(n, with_emb) for n in nodes]
    async with acquire(nodes[0].client_id) as conn:
        await conn.executemany(sql, rows)


async def insert_areps(reps: list[ARep]) -> None:
    if not reps:
        return
    s = _schema()
    with_emb = await pgvector_available()
    cols = list(_AREP_COLS) + (["rep_embedding"] if with_emb else [])
    ph = []
    for i, c in enumerate(cols, start=1):
        if c == "path":
            ph.append(f"${i}::ltree")
        elif c == "rep_embedding":
            ph.append(f"${i}::vector")
        else:
            ph.append(f"${i}")
    sql = f'INSERT INTO "{s}".arep ({", ".join(cols)}) VALUES ({", ".join(ph)})'

    def _row(r: ARep) -> tuple:
        base = (_new_id(r.id), r.knode_id, r.client_id, r.doc_id, r.version_id, r.path,
                r.rep_type.value, r.rep_lang, r.rep_text, r.gen_model)
        return (*base, vec_to_pg(r.embedding)) if with_emb else base

    async with acquire(reps[0].client_id) as conn:
        await conn.executemany(sql, [_row(r) for r in reps])


async def upsert_merged_facts(facts: list[ClientFact]) -> None:
    if not facts:
        return
    s = _schema()
    async with acquire(facts[0].client_id) as conn:
        await conn.executemany(
            f'INSERT INTO "{s}".client_merged_fact '
            "(id, client_id, attribute_key, resolved_value, value_date, value_num, confidence, "
            " conflict, needs_review, source_fact_ids) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
            "ON CONFLICT (client_id, attribute_key) DO UPDATE SET "
            " resolved_value=EXCLUDED.resolved_value, value_date=EXCLUDED.value_date, "
            " value_num=EXCLUDED.value_num, confidence=EXCLUDED.confidence, "
            " conflict=EXCLUDED.conflict, needs_review=EXCLUDED.needs_review, "
            " source_fact_ids=EXCLUDED.source_fact_ids, updated_at=now()",
            [(str(uuid.uuid4()), f.client_id, f.attribute_key, f.resolved_value, f.value_date,
              f.value_num, f.confidence, f.conflict, f.needs_review,
              [uuid.UUID(x) for x in f.source_fact_ids]) for f in facts],
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _current_clause(s: str, alias: str = "k") -> str:
    """Predicate restricting to nodes whose version is current."""
    return (f"{alias}.version_id IN (SELECT id FROM \"{s}\".doc_version dv "
            f"WHERE dv.client_id = {alias}.client_id AND dv.is_current)")


async def fetch_subtree(client_id: str, *, doc_id: str | None = None, path_prefix: str | None = None,
                        max_depth: int | None = None, current_only: bool = True,
                        ) -> list[dict[str, Any]]:
    s = _schema()
    conds = ["k.client_id = $1", "k.deleted_at IS NULL"]
    params: list[Any] = [client_id]
    if doc_id:
        params.append(doc_id)
        conds.append(f"k.doc_id = ${len(params)}")
    if path_prefix:
        params.append(path_prefix)
        conds.append(f"k.path <@ ${len(params)}::ltree")
    if max_depth is not None:
        params.append(max_depth)
        conds.append(f"k.depth <= ${len(params)}")
    if current_only:
        conds.append(_current_clause(s))
    sql = (f'SELECT * FROM "{s}".knode k WHERE ' + " AND ".join(conds)
           + " ORDER BY k.path, k.seq")
    async with acquire(client_id) as conn:
        return [dict(r) for r in await conn.fetch(sql, *params)]


async def fetch_node(client_id: str, node_id: str) -> dict[str, Any] | None:
    s = _schema()
    async with acquire(client_id) as conn:
        row = await conn.fetchrow(
            f'SELECT * FROM "{s}".knode WHERE client_id = $1 AND id = $2', client_id, node_id)
        return dict(row) if row else None


async def list_documents(client_id: str) -> list[dict[str, Any]]:
    s = _schema()
    async with acquire(client_id) as conn:
        return [dict(r) for r in await conn.fetch(
            f'SELECT * FROM "{s}".di_documents WHERE client_id = $1 AND deleted_at IS NULL '
            "ORDER BY created_at DESC", client_id)]


async def record_decision_trace(client_id: str, doc_id: str | None, gate: GateResult) -> None:
    s = _schema()
    async with acquire(client_id) as conn:
        await conn.execute(
            f'INSERT INTO "{s}".di_decision_trace '
            "(id, client_id, doc_id, classification, pii_entities, sensitivity, gate_decision, "
            " lang_profile) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            str(uuid.uuid4()), client_id, doc_id, gate.classification.model_dump(mode="json"),
            [e.model_dump(mode="json") for e in gate.pii_entities], gate.sensitivity.value,
            gate.decision.value, gate.lang_profile.model_dump(mode="json"),
        )


# ---------------------------------------------------------------------------
# Hybrid search (index-many / return-parent, RRF fusion, vector-optional)
# ---------------------------------------------------------------------------
def _rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, node_id in enumerate(ranking, start=1):
            scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (k + rank)
    return scores


async def hybrid_search(client_id: str, *, query_text: str, query_embedding: list[float] | None = None,
                        scope_path: str | None = None, doc_id: str | None = None,
                        top_k: int = 20, current_only: bool = True) -> list[dict[str, Any]]:
    """Return ranked ``knode`` rows. Searches knode.content_tsv + arep.rep_tsv (lexical) and, when
    pgvector is present and an embedding is supplied, the vector legs too; fuses by RRF."""
    s = _schema()
    has_vec = await pgvector_available() and query_embedding is not None
    pool_n = max(top_k * 5, 50)

    def _scope(alias: str, params: list[Any]) -> str:
        conds = [f"{alias}.client_id = $1"]
        if alias == "k":
            conds.append("k.deleted_at IS NULL")
        if doc_id:
            params.append(doc_id)
            conds.append(f"{alias}.doc_id = ${len(params)}")
        if scope_path:
            params.append(scope_path)
            conds.append(f"{alias}.path <@ ${len(params)}::ltree")
        if current_only:
            conds.append(_current_clause(s, alias))
        return " AND ".join(conds)

    rankings: list[list[str]] = []
    async with acquire(client_id) as conn:
        # Lexical leg over knode
        p: list[Any] = [client_id]
        where = _scope("k", p)
        p.append(query_text)
        knode_lex = await conn.fetch(
            f'SELECT k.id FROM "{s}".knode k WHERE {where} '
            f"AND k.content_tsv @@ websearch_to_tsquery('simple', ${len(p)}) "
            f"ORDER BY ts_rank(k.content_tsv, websearch_to_tsquery('simple', ${len(p)})) DESC "
            f"LIMIT {pool_n}", *p)
        rankings.append([str(r["id"]) for r in knode_lex])

        # Lexical leg over arep -> parent knode_id
        p = [client_id]
        where = _scope("a", p)
        p.append(query_text)
        arep_lex = await conn.fetch(
            f'SELECT a.knode_id FROM "{s}".arep a WHERE {where} '
            f"AND a.rep_tsv @@ websearch_to_tsquery('simple', ${len(p)}) "
            f"ORDER BY ts_rank(a.rep_tsv, websearch_to_tsquery('simple', ${len(p)})) DESC "
            f"LIMIT {pool_n}", *p)
        rankings.append([str(r["knode_id"]) for r in arep_lex])

        if has_vec:
            vec = vec_to_pg(query_embedding)
            p = [client_id]
            where = _scope("k", p)
            p.append(vec)
            knode_vec = await conn.fetch(
                f'SELECT k.id FROM "{s}".knode k WHERE {where} AND k.content_embedding IS NOT NULL '
                f"ORDER BY k.content_embedding <=> ${len(p)}::vector LIMIT {pool_n}", *p)
            rankings.append([str(r["id"]) for r in knode_vec])

            p = [client_id]
            where = _scope("a", p)
            p.append(vec)
            arep_vec = await conn.fetch(
                f'SELECT a.knode_id FROM "{s}".arep a WHERE {where} AND a.rep_embedding IS NOT NULL '
                f"ORDER BY a.rep_embedding <=> ${len(p)}::vector LIMIT {pool_n}", *p)
            rankings.append([str(r["knode_id"]) for r in arep_vec])

        fused = _rrf(rankings)
        if not fused:
            return []
        top_ids = [nid for nid, _ in sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]]
        rows = await conn.fetch(
            f'SELECT * FROM "{s}".knode WHERE client_id = $1 AND id = ANY($2::uuid[])',
            client_id, [uuid.UUID(x) for x in top_ids])
    by_id = {str(r["id"]): dict(r) for r in rows}
    ordered = [by_id[i] for i in top_ids if i in by_id]
    for rank, row in enumerate(ordered, start=1):
        row["_rank"] = rank
        row["_score"] = fused[str(row["id"])]
    return ordered


# expose for callers that need the live dim
__all__ = [
    "find_document", "insert_document", "get_current_version", "create_version",
    "insert_knodes", "insert_areps", "upsert_merged_facts", "fetch_subtree", "fetch_node",
    "list_documents", "record_decision_trace", "hybrid_search", "embedding_dim",
]
