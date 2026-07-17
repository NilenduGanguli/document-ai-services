"""FastMCP server construction — the read/search/ingest tool surface for agents.

:func:`build_mcp` returns a configured :class:`~mcp.server.fastmcp.FastMCP` whose
``streamable_http_app()`` is mounted at ``/mcp`` by :func:`di.app.create_app`. The server is
**stateless** (``stateless_http=True``) with plain JSON responses: every tool is an independent
request/response, so no per-session state has to be persisted or reaped.

Every tool follows the same shape:

1. ``await mcp_auth.require(ctx, scope=..., client_id=...)`` — authenticate the ``X-API-KEY`` and
   authorize the tenant/scope (identical to the REST dependency chain).
2. delegate to ``di.store`` / ``di.jobs`` with ``client_id`` — so the read runs under
   ``acquire(client_id)`` and RLS filters to that tenant only.
3. project through ``di.serving`` with the server-side masking default — so sensitive values are
   redacted unless the caller has clearance and opts out, exactly as over REST.

Admin/destructive operations (adjudicate, purge, key management) are intentionally absent: a
one-tool-call irreversible tenant mutation is not an acceptable agent affordance. They can live
behind a separate, explicitly-gated admin MCP server if a workflow ever needs them.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from di import jobs, serving, store
from di.config import get_settings
from di.db import pgvector_available
from di.mcp import auth as mcp_auth
from di.retrieval_client import get_retrieval_client
from di.subtree import versioning

logger = logging.getLogger(__name__)


def _mask_default(mask: bool | None) -> bool:
    return get_settings().mask_by_default if mask is None else mask


async def _embed_query(query: str) -> list[float] | None:
    """Embed a search query through the model gateway when pgvector is available (else None →
    lexical-only search). Mirrors di/routers/search.py so MCP and REST search behave identically."""
    if not await pgvector_available():
        return None
    client = get_retrieval_client()
    try:
        vecs = await client.embed([query])
        return vecs[0] if vecs else None
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()


def build_mcp() -> FastMCP:
    """Construct the MCP server and register every tool. Called once by di.app.create_app."""
    mcp = FastMCP(
        name="document-ai-services",
        instructions=(
            "Per-client KYC knowledge trees from documents. Every tool needs a client_id (the "
            "tenant) and the same X-API-KEY you would use over REST; results are tenant-isolated "
            "and sensitive values are masked by default."
        ),
        stateless_http=True,
        json_response=True,
        # The sub-app's own route sits at its root, so mounting the returned ASGI app at "/mcp"
        # (di.app._maybe_mount_mcp) makes the full public path exactly /mcp — not /mcp/mcp, which
        # the default streamable_http_path="/mcp" would produce under a mount (→ 405/404).
        streamable_http_path="/",
    )

    # ---------------------------------------------------------------- reads
    @mcp.tool()
    async def search_knowledge(
        client_id: str,
        query: str,
        ctx: Context,
        top_k: int = 20,
        scope_path: str | None = None,
        doc_id: str | None = None,
        current_only: bool = True,
        mask: bool | None = None,
    ) -> dict[str, Any]:
        """Hybrid (lexical + vector) search across a client's knowledge tree, grounded in sources.

        Returns ranked knode hits. ``scope_path``/``doc_id`` narrow the search; ``top_k`` is capped
        by the server. When pgvector is unavailable the result's ``vector`` is False (lexical-only).
        """
        await mcp_auth.require(ctx, scope="read", client_id=client_id)
        settings = get_settings()
        masked = _mask_default(mask)
        top_k = min(max(1, top_k), settings.max_top_k)
        query_embedding = await _embed_query(query)
        hits = await store.hybrid_search(
            client_id, query_text=query, query_embedding=query_embedding,
            scope_path=scope_path, doc_id=doc_id, top_k=top_k, current_only=current_only)
        return {
            "client_id": client_id, "query": query, "count": len(hits), "masked": masked,
            "vector": query_embedding is not None,
            "hits": serving.project_nodes(hits, mask=masked),
        }

    @mcp.tool()
    async def get_client_facts(
        client_id: str,
        ctx: Context,
        attribute_key: str | None = None,
        verified_only: bool = False,
        mask: bool | None = None,
    ) -> dict[str, Any]:
        """The client's merged, cross-document facts (the current view).

        ``verified_only`` keeps only independently-verified facts (checksum/registry/human), not
        self-scored LLM output. Multi-valued attributes (directors, beneficial owners, accounts)
        return several rows sharing one ``attribute_key`` — distinguish them by ``instance_key``.
        """
        await mcp_auth.require(ctx, scope="read", client_id=client_id)
        masked = _mask_default(mask)
        rows = await store.fetch_merged_facts(client_id, attribute_key=attribute_key)
        facts = serving.project_facts(rows, mask=masked, verified_only=verified_only)
        return {"client_id": client_id, "count": len(facts), "masked": masked, "facts": facts}

    @mcp.tool()
    async def get_document_tree(
        client_id: str,
        ctx: Context,
        doc_id: str | None = None,
        path: str | None = None,
        max_depth: int | None = None,
        current_only: bool = True,
        mask: bool | None = None,
    ) -> dict[str, Any]:
        """The nested knowledge subtree for a client, optionally scoped to one document or path."""
        await mcp_auth.require(ctx, scope="read", client_id=client_id)
        masked = _mask_default(mask)
        rows = await store.fetch_subtree(client_id, doc_id=doc_id, path_prefix=path,
                                         max_depth=max_depth, current_only=current_only)
        return {"client_id": client_id, "count": len(rows), "masked": masked,
                "tree": serving.nest_tree(rows, mask=masked)}

    @mcp.tool()
    async def list_client_documents(
        client_id: str,
        ctx: Context,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List a client's documents (keyset-paginated; raw OCR text is never included)."""
        await mcp_auth.require(ctx, scope="read", client_id=client_id)
        docs, next_cursor = await store.list_documents(
            client_id, limit=store.clamp_limit(limit), cursor=cursor)
        return {"client_id": client_id, "count": len(docs),
                "documents": [{**d, "id": str(d["id"])} for d in docs], "next_cursor": next_cursor}

    @mcp.tool()
    async def get_document_manifest(client_id: str, doc_id: str, ctx: Context) -> dict[str, Any]:
        """What a specific document can answer, and how (its capabilities manifest)."""
        await mcp_auth.require(ctx, scope="read", client_id=client_id)
        doc = await store.get_document(client_id, doc_id)
        if doc is None:
            raise ValueError(f"document not found: {doc_id}")
        nodes = await store.fetch_subtree(client_id, doc_id=doc_id)
        reps = await store.fetch_areps(client_id, doc_id=doc_id)
        return serving.build_manifest(doc, nodes, reps)

    @mcp.tool()
    async def get_answerable_questions(client_id: str, doc_id: str, ctx: Context) -> dict[str, Any]:
        """The questions a document can answer, derived from its accessibility representations."""
        await mcp_auth.require(ctx, scope="read", client_id=client_id)
        reps = await store.fetch_areps(client_id, doc_id=doc_id)
        return {"client_id": client_id, "doc_id": doc_id,
                "answerable": serving.answerable_questions(reps)}

    @mcp.tool()
    async def get_node_provenance(client_id: str, node_id: str, ctx: Context) -> dict[str, Any]:
        """Trace one node back to its exact source: document, page, bounding box, extractor, model."""
        await mcp_auth.require(ctx, scope="read", client_id=client_id)
        node = await store.fetch_node(client_id, node_id)
        if node is None:
            raise ValueError(f"node not found: {node_id}")
        return {
            "node_id": node_id, "client_id": client_id,
            "doc_id": str(node.get("doc_id")) if node.get("doc_id") else None,
            "version_id": str(node.get("version_id")) if node.get("version_id") else None,
            "node_type": node.get("node_type"), "attribute_key": node.get("attribute_key"),
            "verification_status": node.get("verification_status"),
            "confidence": node.get("confidence"), "provenance": node.get("provenance"),
        }

    @mcp.tool()
    async def get_job_status(client_id: str, job_id: str, ctx: Context) -> dict[str, Any]:
        """Poll an ingest job: status, current stage, every stage event, and any error."""
        await mcp_auth.require(ctx, scope="read", client_id=client_id)
        job = await jobs.get_job(client_id, job_id)
        if job is None:
            raise ValueError(f"job not found: {job_id}")
        return job.model_dump(mode="json")

    # ---------------------------------------------------------------- ingest
    @mcp.tool()
    async def submit_ingest(
        client_id: str,
        filename: str,
        content_base64: str,
        ctx: Context,
        mime: str | None = None,
        idempotency_key: str | None = None,
        external_document_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit a document for ingestion. Returns a job handle to poll with get_job_status.

        ``content_base64`` is the raw document bytes, base64-encoded. The same admission rules as
        the REST accept path apply: durable blob storage required, per-tenant quota, size cap, and
        idempotency (a repeat with the same ``idempotency_key`` returns the existing job).
        """
        # Local imports: reuse the REST accept-path guards without a module-load coupling.
        from fastapi import HTTPException

        from di.routers.ingest import _enforce_ingest_quota, _require_durable_blob_backend
        from di.storage import BlobStoreError, blob_key, get_blob_store

        await mcp_auth.require(ctx, scope="ingest", client_id=client_id)
        settings = get_settings()

        try:
            content = base64.b64decode(content_base64, validate=True)
        except Exception as exc:  # noqa: BLE001 - malformed base64 is a caller error
            raise ValueError(f"content_base64 is not valid base64: {exc}") from exc
        if not content:
            raise ValueError("empty document")
        if len(content) > settings.max_upload_bytes:
            raise ValueError(
                f"file exceeds the {settings.max_upload_mb} MB limit "
                f"({len(content) / 1024 / 1024:.1f} MB)")

        # Idempotency BEFORE quota: a retry of an already-accepted submit must never be throttled.
        if idempotency_key:
            existing = await jobs.find_by_idempotency(client_id, idempotency_key)
            if existing is not None:
                return {"job_id": existing.id, "client_id": client_id,
                        "status": existing.status.value, "document_name": existing.document_name,
                        "reused": True}

        try:
            await _enforce_ingest_quota(client_id)
            _require_durable_blob_backend()
        except HTTPException as exc:
            raise ValueError(exc.detail) from exc

        content_hash = versioning.content_hash(content)
        try:
            ref = await get_blob_store().put(
                client_id=client_id, key=blob_key(client_id, content_hash, filename),
                data=content, content_type=mime)
        except BlobStoreError as exc:
            raise ValueError(f"could not durably store the upload: {exc}") from exc

        job = await jobs.enqueue(
            client_id=client_id, kind="ingest", document_name=filename,
            idempotency_key=idempotency_key,
            payload={
                "blob_uri": ref.uri, "blob_backend": ref.backend, "content_hash": content_hash,
                "filename": filename, "mime": mime, "external_document_id": external_document_id,
                "created_by": f"mcp:{client_id}", "size": len(content),
            })
        return {"job_id": job.id, "client_id": client_id, "status": job.status.value,
                "document_name": filename, "reused": False}

    return mcp


__all__ = ["build_mcp"]
