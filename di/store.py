"""Persistence / repository layer for the knowledge tree.

All access goes through ``di.db.acquire(client_id)`` so the RLS tenant GUC is bound for the
checkout. Vector search degrades gracefully to full-text search when pgvector is absent. The
hybrid search implements the "index-many / return-parent" pattern: it searches both ``knode``
content and ``arep`` representations, maps ``arep`` hits back to their parent ``knode``, and
fuses the legs with Reciprocal Rank Fusion.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime
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
# Keyset pagination
# ---------------------------------------------------------------------------
def encode_cursor(created_at: datetime, row_id: str) -> str:
    """Opaque cursor for keyset pagination over (created_at DESC, id DESC)."""
    raw = f"{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Inverse of :func:`encode_cursor`. Raises ValueError on a malformed cursor."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        ts, sep, row_id = raw.partition("|")
        if not sep or not row_id:
            raise ValueError("missing separator")
        return datetime.fromisoformat(ts), row_id
    except Exception as exc:  # noqa: BLE001 - normalize every decode failure to ValueError
        raise ValueError(f"malformed cursor: {cursor!r}") from exc


def clamp_limit(limit: int | None) -> int:
    """Clamp a caller-supplied page size into [1, max_page_size]."""
    settings = get_settings()
    if limit is None:
        return settings.default_page_size
    return max(1, min(int(limit), settings.max_page_size))


def _paginate(rows: list[dict[str, Any]], limit: int,
              ) -> tuple[list[dict[str, Any]], str | None]:
    """Trim an over-fetched page (limit+1) and derive the next cursor."""
    has_more = len(rows) > limit
    page = rows[:limit]
    if not (has_more and page):
        return page, None
    last = page[-1]
    return page, encode_cursor(last["created_at"], str(last["id"]))


# ---------------------------------------------------------------------------
# Documents & versions
# ---------------------------------------------------------------------------
async def find_document(client_id: str, document_name: str,
                        external_document_id: str | None = None) -> dict[str, Any] | None:
    """Resolve a logical document.

    ``external_document_id`` is the caller's own identity for the document and takes precedence:
    a source system that names every scan ``scan001.pdf`` would otherwise have each upload
    silently supersede an unrelated document.
    """
    s = _schema()
    async with acquire(client_id) as conn:
        if external_document_id:
            row = await conn.fetchrow(
                f'SELECT * FROM "{s}".di_documents '
                "WHERE client_id = $1 AND external_document_id = $2 AND deleted_at IS NULL",
                client_id, external_document_id,
            )
            return dict(row) if row else None
        row = await conn.fetchrow(
            f'SELECT * FROM "{s}".di_documents '
            "WHERE client_id = $1 AND document_name = $2 AND external_document_id IS NULL "
            "AND deleted_at IS NULL",
            client_id, document_name,
        )
        return dict(row) if row else None


async def insert_document(meta: DocumentMeta, *, ocr_text: str | None = None,
                          ocr_lines: list[dict] | None = None,
                          lang_profile: dict | None = None) -> str:
    """Insert (or UPSERT) a document row; returns its id.

    The conflict target is (client_id, document_name) when no ``external_document_id`` is given,
    and (client_id, external_document_id) when one is — so the caller's identity wins.
    """
    s = _schema()
    doc_id = _new_id(meta.id)
    updates = (
        " s3_uri=EXCLUDED.s3_uri, sha256=EXCLUDED.sha256, mime=EXCLUDED.mime, "
        " doc_type=EXCLUDED.doc_type, doc_category=EXCLUDED.doc_category, subject=EXCLUDED.subject, "
        " jurisdiction=EXCLUDED.jurisdiction, lang_profile=EXCLUDED.lang_profile, "
        " sensitivity_bucket=EXCLUDED.sensitivity_bucket, gate_decision=EXCLUDED.gate_decision, "
        " confidence=EXCLUDED.confidence, ocr_engine=EXCLUDED.ocr_engine, "
        " page_count=EXCLUDED.page_count, ocr_text=EXCLUDED.ocr_text, ocr_lines=EXCLUDED.ocr_lines, "
        " blob_uri=EXCLUDED.blob_uri, blob_backend=EXCLUDED.blob_backend, "
        " document_name=EXCLUDED.document_name, updated_at=now(), deleted_at=NULL "
    )
    target = (
        "(client_id, external_document_id) WHERE external_document_id IS NOT NULL"
        if meta.external_document_id
        else "(client_id, document_name)"
    )
    async with acquire(meta.client_id) as conn:
        row = await conn.fetchrow(
            f'INSERT INTO "{s}".di_documents '
            "(id, client_id, document_name, external_document_id, s3_uri, blob_uri, blob_backend, "
            " sha256, mime, doc_type, doc_category, subject, jurisdiction, lang_profile, "
            " sensitivity_bucket, gate_decision, confidence, ocr_engine, page_count, ocr_text, "
            " ocr_lines) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21) "
            f"ON CONFLICT {target} DO UPDATE SET {updates}"
            "RETURNING id",
            doc_id, meta.client_id, meta.document_name, meta.external_document_id, meta.s3_uri,
            meta.blob_uri, meta.blob_backend, meta.sha256, meta.mime, meta.doc_type,
            meta.doc_category, meta.subject, meta.jurisdiction, lang_profile or {},
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
    """Persist the merged client view, including the winner's verification + resolution evidence."""
    if not facts:
        return
    s = _schema()
    async with acquire(facts[0].client_id) as conn:
        await conn.executemany(
            f'INSERT INTO "{s}".client_merged_fact '
            "(id, client_id, attribute_key, resolved_value, value_date, value_num, confidence, "
            " conflict, needs_review, source_fact_ids, verification_status, winning_fact_id, "
            " resolution_rationale, ontology_version, adjudicated) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) "
            "ON CONFLICT (client_id, attribute_key) DO UPDATE SET "
            " resolved_value=EXCLUDED.resolved_value, value_date=EXCLUDED.value_date, "
            " value_num=EXCLUDED.value_num, confidence=EXCLUDED.confidence, "
            " conflict=EXCLUDED.conflict, needs_review=EXCLUDED.needs_review, "
            " source_fact_ids=EXCLUDED.source_fact_ids, "
            " verification_status=EXCLUDED.verification_status, "
            " winning_fact_id=EXCLUDED.winning_fact_id, "
            " resolution_rationale=EXCLUDED.resolution_rationale, "
            " ontology_version=EXCLUDED.ontology_version, adjudicated=EXCLUDED.adjudicated, "
            " updated_at=now()",
            [(str(uuid.uuid4()), f.client_id, f.attribute_key, f.resolved_value, f.value_date,
              f.value_num, f.confidence, f.conflict, f.needs_review,
              [uuid.UUID(x) for x in f.source_fact_ids],
              f.verification_status.value if f.verification_status else None,
              uuid.UUID(f.winning_fact_id) if f.winning_fact_id else None,
              f.resolution_rationale or {}, f.ontology_version, f.adjudicated) for f in facts],
        )


# ---------------------------------------------------------------------------
# Human adjudication (survives re-merge)
# ---------------------------------------------------------------------------
async def fetch_adjudications(client_id: str) -> list[dict[str, Any]]:
    s = _schema()
    async with acquire(client_id) as conn:
        return [dict(r) for r in await conn.fetch(
            f'SELECT * FROM "{s}".di_fact_adjudication WHERE client_id = $1', client_id)]


async def upsert_adjudication(client_id: str, *, attribute_key: str, verdict: str,
                              value_text: str | None = None, value_date: Any = None,
                              value_num: float | None = None, reviewer: str | None = None,
                              note: str | None = None) -> None:
    """Record a reviewer's decision for an attribute. Re-merge reapplies it, so it is not lost."""
    s = _schema()
    async with acquire(client_id) as conn:
        await conn.execute(
            f'INSERT INTO "{s}".di_fact_adjudication '
            "(id, client_id, attribute_key, verdict, value_text, value_date, value_num, reviewer, "
            " note) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
            "ON CONFLICT (client_id, attribute_key) DO UPDATE SET "
            " verdict=EXCLUDED.verdict, value_text=EXCLUDED.value_text, "
            " value_date=EXCLUDED.value_date, value_num=EXCLUDED.value_num, "
            " reviewer=EXCLUDED.reviewer, note=EXCLUDED.note, created_at=now()",
            str(uuid.uuid4()), client_id, attribute_key, verdict, value_text, value_date,
            value_num, reviewer, note,
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


#: Columns returned by the document LIST endpoint. Deliberately excludes ocr_text / ocr_lines:
#: shipping every page's raw OCR (and its PII) in a list response is both a payload and a
#: disclosure problem. Fetch a single document to get the full OCR payload.
_DOC_LIST_COLS = (
    "id, client_id, document_name, external_document_id, doc_type, doc_category, subject, "
    "jurisdiction, sensitivity_bucket, gate_decision, confidence, ocr_engine, page_count, "
    "sha256, mime, blob_backend, created_at, updated_at"
)


async def list_documents(client_id: str, *, limit: int = 50, cursor: str | None = None,
                         ) -> tuple[list[dict[str, Any]], str | None]:
    """Keyset-paginated document list. Cursor is an opaque "created_at|id"."""
    s = _schema()
    conds = ["client_id = $1", "deleted_at IS NULL"]
    params: list[Any] = [client_id]
    if cursor:
        created_at, cid = decode_cursor(cursor)
        params.extend([created_at, cid])
        conds.append(f"(created_at, id) < (${len(params) - 1}::timestamptz, ${len(params)}::uuid)")
    sql = (f"SELECT {_DOC_LIST_COLS} FROM \"{s}\".di_documents WHERE " + " AND ".join(conds)
           + f" ORDER BY created_at DESC, id DESC LIMIT {int(limit) + 1}")
    async with acquire(client_id) as conn:
        rows = [dict(r) for r in await conn.fetch(sql, *params)]
    return _paginate(rows, limit)


async def fetch_client_facts(client_id: str) -> list[dict[str, Any]]:
    """Fetch only the current *fact* nodes, with the narrow column set the merge needs.

    The merge previously pulled the client's entire subtree (``SELECT *`` over every knode,
    hauling content, tsvectors and embeddings) on every ingest, so cost scaled with tenant size
    rather than document size. The ``knode_attr`` partial index serves this predicate directly.
    """
    s = _schema()
    sql = (
        "SELECT k.id, k.attribute_key, k.value_text, k.value_date, k.value_num, k.confidence, "
        "       k.verification_status "
        f'FROM "{s}".knode k '
        "WHERE k.client_id = $1 AND k.deleted_at IS NULL "
        "  AND k.node_type = 'fact' AND k.attribute_key IS NOT NULL "
        f"  AND {_current_clause(s)}"
    )
    async with acquire(client_id) as conn:
        return [dict(r) for r in await conn.fetch(sql, client_id)]


async def get_document(client_id: str, doc_id: str) -> dict[str, Any] | None:
    s = _schema()
    async with acquire(client_id) as conn:
        row = await conn.fetchrow(
            f'SELECT * FROM "{s}".di_documents WHERE client_id = $1 AND id = $2 '
            "AND deleted_at IS NULL", client_id, doc_id)
        return dict(row) if row else None


async def fetch_merged_facts(client_id: str, *, attribute_key: str | None = None,
                             ) -> list[dict[str, Any]]:
    s = _schema()
    sql = f'SELECT * FROM "{s}".client_merged_fact WHERE client_id = $1'
    params: list[Any] = [client_id]
    if attribute_key:
        params.append(attribute_key)
        sql += f" AND attribute_key = ${len(params)}"
    sql += " ORDER BY attribute_key"
    async with acquire(client_id) as conn:
        return [dict(r) for r in await conn.fetch(sql, *params)]


async def fetch_areps(client_id: str, *, doc_id: str | None = None, knode_id: str | None = None,
                      current_only: bool = True) -> list[dict[str, Any]]:
    s = _schema()
    conds = ["a.client_id = $1"]
    params: list[Any] = [client_id]
    if doc_id:
        params.append(doc_id)
        conds.append(f"a.doc_id = ${len(params)}")
    if knode_id:
        params.append(knode_id)
        conds.append(f"a.knode_id = ${len(params)}")
    if current_only:
        conds.append("a.version_id IN (SELECT id FROM \"" + s + "\".doc_version dv "
                     "WHERE dv.client_id = a.client_id AND dv.is_current)")
    sql = f'SELECT * FROM "{s}".arep a WHERE ' + " AND ".join(conds)
    async with acquire(client_id) as conn:
        return [dict(r) for r in await conn.fetch(sql, *params)]


async def list_version_changes(client_id: str, *, since: str | None = None,
                               after_seq: int | None = None, limit: int = 50,
                               ) -> tuple[list[dict[str, Any]], int | None]:
    """Version delta feed.

    ``after_seq`` is the preferred cursor: ``change_seq`` is a monotonic sequence, so a consumer
    can resume exactly where it stopped. ``since`` (a timestamp) is retained for compatibility but
    is inclusive (``>=``) and therefore re-delivers rows sharing a boundary timestamp.
    Returns (rows, next_seq) where next_seq is the highest change_seq in the page.
    """
    s = _schema()
    sql = (f'SELECT v.*, d.document_name, d.doc_type FROM "{s}".doc_version v '
           f'JOIN "{s}".di_documents d ON d.id = v.doc_id AND d.client_id = v.client_id '
           "WHERE v.client_id = $1")
    params: list[Any] = [client_id]
    if after_seq is not None:
        params.append(after_seq)
        sql += f" AND v.change_seq > ${len(params)}"
    elif since:
        params.append(since)
        sql += f" AND v.created_at >= ${len(params)}::timestamptz"
    order = "v.change_seq ASC" if after_seq is not None else "v.created_at DESC"
    sql += f" ORDER BY {order} LIMIT {int(limit)}"
    async with acquire(client_id) as conn:
        rows = [dict(r) for r in await conn.fetch(sql, *params)]
    seqs = [r["change_seq"] for r in rows if r.get("change_seq") is not None]
    return rows, (max(seqs) if seqs else None)


async def record_decision_trace(client_id: str, doc_id: str | None, gate: GateResult) -> None:
    """Persist the per-document gate audit row, including *why* it was routed that way."""
    s = _schema()
    anchor_summary = {
        "signals": list(gate.classification.signals or []),
        "confidence": gate.classification.confidence,
        "pii_types": sorted({e.entity_type for e in gate.pii_entities}),
    }
    async with acquire(client_id) as conn:
        await conn.execute(
            f'INSERT INTO "{s}".di_decision_trace '
            "(id, client_id, doc_id, classification, pii_entities, sensitivity, gate_decision, "
            " lang_profile, rationale, anchor_summary) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
            str(uuid.uuid4()), client_id, doc_id, gate.classification.model_dump(mode="json"),
            [e.model_dump(mode="json") for e in gate.pii_entities], gate.sensitivity.value,
            gate.decision.value, gate.lang_profile.model_dump(mode="json"),
            gate.rationale or None, anchor_summary,
        )


# ---------------------------------------------------------------------------
# Deletion / erasure (retention, right-to-erasure, tenant off-boarding)
# ---------------------------------------------------------------------------
async def delete_document(client_id: str, doc_id: str) -> dict[str, int]:
    """Hard-delete one document and everything derived from it.

    Only ``doc_version`` cascades from ``di_documents``; knode/arep/decision-trace rows must be
    removed explicitly. The merged client view is recomputed by the caller afterwards, since
    ``client_merged_fact.source_fact_ids`` has no FK to cascade through.
    """
    s = _schema()
    counts: dict[str, int] = {}
    async with acquire(client_id) as conn:
        async with conn.transaction():
            for table in ("arep", "knode", "di_decision_trace"):
                res = await conn.execute(
                    f'DELETE FROM "{s}".{table} WHERE client_id = $1 AND doc_id = $2',
                    client_id, doc_id)
                counts[table] = int(res.split()[-1]) if res else 0
            res = await conn.execute(
                f'DELETE FROM "{s}".di_documents WHERE client_id = $1 AND id = $2',
                client_id, doc_id)  # doc_version cascades
            counts["di_documents"] = int(res.split()[-1]) if res else 0
    return counts


async def purge_client(client_id: str) -> dict[str, int]:
    """Erase every trace of a tenant (off-boarding / right-to-erasure). Irreversible."""
    s = _schema()
    tables = ("arep", "knode", "doc_version", "client_merged_fact", "di_fact_adjudication",
              "di_decision_trace", "di_entity", "di_job", "di_blob", "di_documents")
    counts: dict[str, int] = {}
    async with acquire(client_id) as conn:
        async with conn.transaction():
            for table in tables:
                res = await conn.execute(
                    f'DELETE FROM "{s}".{table} WHERE client_id = $1', client_id)
                counts[table] = int(res.split()[-1]) if res else 0
    return counts


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
    "clamp_limit", "create_version", "decode_cursor", "delete_document", "embedding_dim",
    "encode_cursor", "fetch_adjudications", "fetch_areps", "fetch_client_facts",
    "fetch_merged_facts", "fetch_node", "fetch_subtree", "find_document", "get_current_version",
    "get_document", "hybrid_search", "insert_areps", "insert_document", "insert_knodes",
    "list_documents", "list_version_changes", "purge_client", "record_decision_trace",
    "upsert_adjudication", "upsert_merged_facts",
]
