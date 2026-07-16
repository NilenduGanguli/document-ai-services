"""End-to-end ingestion driver.

``ingest_document`` orchestrates the full pipeline and yields :class:`~di.models.IngestEvent`
stages (the router turns these into SSE). Model access (embeddings, LLM, aids) goes through the
retrieval gateway; deterministic checksum extraction always runs locally; LLM-generated context
prefixes + accessibility reps run only for documents the gate allows out (``SEND_TO_LLM``).
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import anyio

import di.extract.deterministic  # noqa: F401 - import side-effect registers extractors
from di import observability, ontology, serving, store
from di.config import get_settings
from di.db import pgvector_available
from di.extract import base as extract_base
from di.extract import llm_extract
from di.extract.base import ExtractionInput
from di.gate import pipeline as gate_pipeline
from di.models import (
    ARep,
    DocumentMeta,
    ExtractedField,
    GateDecision,
    IngestEvent,
    KNode,
    NodeType,
    OcrResult,
    SensitivityBucket,
)
from di.ocr import vision
from di.retrieval_client import get_retrieval_client
from di.storage import BlobStoreError, blob_key, get_blob_store
from di.subtree import arep as arep_mod
from di.subtree import build, context, merge, versioning

logger = logging.getLogger(__name__)

_CONTENT_TYPES = {NodeType.chunk, NodeType.table, NodeType.figure, NodeType.fact}


def _base_path(client_id: str, doc_type: str | None, version_no: int) -> str:
    cid = build.sanitize_label(f"client_{client_id}")
    dt = build.sanitize_label(f"doctype_{doc_type or 'unknown'}")
    return f"{cid}.{dt}.v{version_no}"


def _node_text(n: KNode) -> str:
    if n.node_type == NodeType.fact:
        body = n.value_text or n.title or ""
        return f"{n.attribute_key or ''}: {body}".strip(": ").strip()
    parts = [p for p in (n.context_prefix, n.content) if p]
    return "\n\n".join(parts)


async def _embed_nodes(nodes: list[KNode], client: Any) -> None:
    targets = [n for n in nodes if n.node_type in _CONTENT_TYPES and _node_text(n)]
    if not targets:
        return
    settings = get_settings()
    batch = settings.embedding_batch_size
    for i in range(0, len(targets), batch):
        chunk = targets[i : i + batch]
        vecs = await client.embed([_node_text(n) for n in chunk])
        for n, v in zip(chunk, vecs, strict=False):
            n.embedding = v


async def _embed_areps(reps: list[ARep], client: Any) -> None:
    if not reps:
        return
    batch = get_settings().embedding_batch_size
    for i in range(0, len(reps), batch):
        chunk = reps[i : i + batch]
        vecs = await client.embed([r.rep_text for r in chunk])
        for r, v in zip(chunk, vecs, strict=False):
            r.embedding = v


async def _remerge_client_facts(client_id: str) -> int:
    """Recompute the client-level merged view from all current fact nodes (confidence-weighted).

    Pushes the fact filter into SQL (``fetch_client_facts``) so this stays proportional to the
    client's fact count rather than to their whole corpus, and reapplies any human adjudications
    so a reviewer's correction survives every subsequent ingest.
    """
    rows = await store.fetch_client_facts(client_id)
    inputs = [
        merge.FactInput(
            fact_id=str(n["id"]),
            attribute_key=n["attribute_key"],
            value=n.get("value_text"),
            value_date=n.get("value_date"),
            value_num=n.get("value_num"),
            confidence=float(n.get("confidence") or 0.0),
            verification_status=n.get("verification_status") or "unverified",
        )
        for n in rows
    ]
    adj_rows = await store.fetch_adjudications(client_id)
    adjudications = {
        (r["attribute_key"], r.get("instance_key") or ""): merge.Adjudication(
            attribute_key=r["attribute_key"], instance_key=r.get("instance_key") or "",
            verdict=r["verdict"], value_text=r.get("value_text"),
            value_date=r.get("value_date"), value_num=r.get("value_num"),
            reviewer=r.get("reviewer"), note=r.get("note"),
        )
        for r in adj_rows
    }
    settings = get_settings()
    merged = merge.merge_facts(
        inputs, client_id=client_id, adjudications=adjudications,
        ontology_version=settings.ontology_version,
        multi_keys=ontology.MULTI_VALUED_ATTRIBUTE_KEYS,
        fingerprint_hmac_key=settings.instance_fingerprint_hmac_key,
    )
    await store.replace_merged_facts(client_id, merged)
    return len(merged)


def _deterministic_facts(doc_type: str | None, ocr: OcrResult) -> list[ExtractedField]:
    if not doc_type:
        return []
    extractor = extract_base.get_extractor(doc_type)
    if extractor is None:
        return []
    try:
        return extractor.extract(ExtractionInput(doc_type, ocr.text, ocr.lines, "en"))
    except Exception:  # noqa: BLE001 - extraction must never break ingest
        logger.exception("deterministic extraction failed for %s", doc_type)
        return []


async def _retain_blob(client_id: str, file_bytes: bytes, content_hash: str, filename: str,
                       mime: str | None) -> tuple[str | None, str | None]:
    """Persist the raw upload to the configured blob backend. Never fatal to ingest."""
    store_ = get_blob_store()
    if store_.backend == "none":
        return None, "none"
    try:
        ref = await store_.put(
            client_id=client_id, key=blob_key(client_id, content_hash, filename),
            data=file_bytes, content_type=mime,
        )
        return ref.uri, ref.backend
    except (BlobStoreError, Exception):  # noqa: BLE001 - retention must not break ingest
        logger.exception("blob retention failed (backend=%s)", store_.backend)
        return None, store_.backend


async def ingest_document(
    client_id: str,
    file_bytes: bytes,
    filename: str,
    *,
    mime: str | None = None,
    created_by: str | None = None,
    external_document_id: str | None = None,
) -> AsyncIterator[IngestEvent]:
    """Async generator yielding pipeline stage events; persists the knowledge subtree.

    Emits a terminal ``error`` event on failure so a consumer can distinguish a real completion
    from a dropped stream, then re-raises for the caller (the job runner) to record.
    """
    settings = get_settings()
    client = get_retrieval_client(settings)
    observability.observe_ingest("started")
    try:
        # Hash FIRST: an unchanged re-upload must no-op without paying for OCR (which can be a
        # ~60s Azure Read round-trip per page).
        content_hash = versioning.content_hash(file_bytes)
        existing = await store.find_document(client_id, filename, external_document_id)
        doc_id = str(existing["id"]) if existing else None
        current = await store.get_current_version(client_id, doc_id) if doc_id else None
        plan = versioning.decide_version(
            content_hash,
            current["version_no"] if current else None,
            current["content_hash"] if current else None,
        )
        if plan.is_noop:
            observability.observe_ingest("noop")
            yield IngestEvent(stage="version", status="skip",
                              detail={"reason": "identical content already current", "doc_id": doc_id})
            yield IngestEvent(stage="done", detail={"doc_id": doc_id, "noop": True})
            return

        yield IngestEvent(stage="ocr", status="start")
        # OCR is fully synchronous (Azure Read polling, pdf2image, Tesseract, pypdf). Running it
        # inline would block the event loop and stall every concurrent read on this instance.
        with observability.stage_timer("ocr"):
            ocr = await anyio.to_thread.run_sync(
                lambda: vision.extract_pages(file_bytes, filename=filename, mime=mime)
            )
        observability.observe_ocr(ocr.engine)
        yield IngestEvent(stage="ocr", detail={"engine": ocr.engine, "pages": ocr.pages})

        blob_uri, blob_backend = await _retain_blob(
            client_id, file_bytes, content_hash, filename, mime)

        yield IngestEvent(stage="gate", status="start")
        with observability.stage_timer("gate"):
            gate = await anyio.to_thread.run_sync(gate_pipeline.run_gate, ocr)
        observability.observe_gate(gate.decision.value, gate.sensitivity.value)
        observability.observe_llm_egress(gate.decision == GateDecision.send_to_llm)
        yield IngestEvent(stage="gate", detail={
            "doc_type": gate.classification.doc_type, "sensitivity": gate.sensitivity.value,
            "decision": gate.decision.value, "lang": gate.lang_profile.dominant_lang})

        # --- Extraction: deterministic always; LLM only when the gate allows it out ---
        yield IngestEvent(stage="extract", status="start")
        facts: list[ExtractedField] = _deterministic_facts(gate.classification.doc_type, ocr)
        allow_llm = gate.decision == GateDecision.send_to_llm
        if allow_llm:
            try:
                _, llm_facts = await llm_extract.classify_and_extract(
                    ocr.text, client=client, doc_type_hint=gate.classification.doc_type)
                facts.extend(llm_facts)
            except Exception:  # noqa: BLE001
                logger.exception("LLM extraction failed; continuing with deterministic facts")
        yield IngestEvent(stage="extract", detail={"facts": len(facts), "llm": allow_llm})

        # The gate scores sensitivity from detected PII entities, which needs the optional [ml]
        # stack. Raise its verdict to whatever we actually extracted, so a passport is never
        # stored as LOW just because no PII model was installed.
        doc_sens_value = serving.document_sensitivity(
            gate.sensitivity.value, [f.attribute_key for f in facts])
        doc_sensitivity = SensitivityBucket(doc_sens_value)

        meta = DocumentMeta(
            id=doc_id, client_id=client_id, document_name=filename,
            external_document_id=external_document_id, sha256=content_hash, mime=mime,
            blob_uri=blob_uri, blob_backend=blob_backend,
            doc_type=gate.classification.doc_type, doc_category=gate.classification.doc_category,
            jurisdiction=gate.classification.jurisdiction, sensitivity_bucket=doc_sensitivity,
            gate_decision=gate.decision, confidence=gate.classification.confidence,
            ocr_engine=ocr.engine, page_count=ocr.pages,
        )
        doc_id = await store.insert_document(
            meta, ocr_text=ocr.text, ocr_lines=[ln.model_dump(mode="json") for ln in ocr.lines],
            lang_profile=gate.lang_profile.model_dump(mode="json"))
        await store.record_decision_trace(client_id, doc_id, gate)
        version_id = await store.create_version(
            client_id, doc_id, content_hash=content_hash, version_no=plan.version_no,
            supersedes_id=str(current["id"]) if current else None, created_by=created_by)

        # --- Build subtree ---
        base = _base_path(client_id, gate.classification.doc_type, plan.version_no)
        nodes = build.build_subtree(
            client_id=client_id, doc_id=doc_id, version_id=version_id,
            classification=gate.classification, ocr=ocr, facts=facts, base_path=base,
            doc_sensitivity=doc_sensitivity)

        # --- Contextual enrichment + embeddings (LLM aids only for SEND_TO_LLM) ---
        if allow_llm:
            try:
                await context.add_context_prefixes(nodes, full_doc_text=ocr.text, client=client)
            except Exception:  # noqa: BLE001
                logger.exception("context-prefix generation failed")
        has_vec = await pgvector_available()
        if has_vec:
            await _embed_nodes(nodes, client)
        await store.insert_knodes(nodes)
        yield IngestEvent(stage="subtree", detail={"nodes": len(nodes), "embedded": has_vec})

        # --- Accessibility representations (LLM-generated; SEND_TO_LLM only) ---
        rep_count = 0
        if allow_llm and not settings.arep_async:
            try:
                reps = await arep_mod.generate_areps(
                    [n for n in nodes if n.node_type in _CONTENT_TYPES], client=client,
                    languages=settings.supported_languages)
                if has_vec:
                    await _embed_areps(reps, client)
                await store.insert_areps(reps)
                rep_count = len(reps)
            except Exception:  # noqa: BLE001
                logger.exception("accessibility-rep generation failed")
        yield IngestEvent(stage="arep", detail={"reps": rep_count,
                                                "deferred": settings.arep_async and allow_llm})

        # --- Cross-document merge into the client-level view ---
        merged = await _remerge_client_facts(client_id)
        yield IngestEvent(stage="merge", detail={"merged_facts": merged})

        observability.observe_ingest("succeeded")
        yield IngestEvent(stage="done", detail={
            "doc_id": doc_id, "version_id": version_id, "version_no": plan.version_no,
            "doc_type": gate.classification.doc_type, "decision": gate.decision.value,
            "nodes": len(nodes), "facts": len(facts), "blob_backend": blob_backend})
    except Exception as exc:
        # A failure used to kill the SSE stream silently after the 200 header; make it explicit
        # and countable, then re-raise so the job runner records the terminal failure.
        observability.observe_ingest("failed")
        logger.exception("ingest failed for client=%s file=%s", client_id, filename)
        yield IngestEvent(stage="error", status="error",
                          detail={"error": str(exc), "type": type(exc).__name__})
        raise
    finally:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
