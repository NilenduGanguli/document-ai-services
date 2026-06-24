"""Serving-layer helpers — pure transforms over stored rows (no DB / no network).

Turns flat ``knode`` rows into a nested tree, applies the toggleable access-aware masking
projection, and derives the per-document capabilities manifest + answerable-questions index.
Kept dependency-free so it is trivially unit-testable.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from di.models import RepType, SensitivityBucket

_MASKABLE = {SensitivityBucket.high.value, SensitivityBucket.critical.value}

# Fields surfaced per node in the API tree.
_NODE_FIELDS = (
    "id", "parent_id", "path", "node_type", "seq", "depth", "title", "content", "context_prefix",
    "attribute_key", "value_text", "value_date", "value_num", "verification_status", "confidence",
    "sensitivity", "valid_from", "valid_to", "provenance", "doc_id", "version_id",
)


def _redact(value: str | None) -> str | None:
    """Mask a sensitive value while keeping a small tail for recognisability."""
    if not value:
        return value
    if len(value) <= 4:
        return "[REDACTED]"
    return "•" * (len(value) - 4) + value[-4:]


def _project_node(row: dict[str, Any], *, mask: bool) -> dict[str, Any]:
    out = {k: row.get(k) for k in _NODE_FIELDS}
    if mask and str(row.get("sensitivity")) in _MASKABLE:
        # Mask only the sensitive payload; structure, provenance, type, confidence stay intact.
        out["value_text"] = _redact(out.get("value_text"))
        if out.get("content"):
            out["content"] = "[REDACTED]"
        out["masked"] = True
    return out


def nest_tree(rows: list[dict[str, Any]], *, mask: bool = False) -> list[dict[str, Any]]:
    """Build a nested tree (roots -> children) from flat knode rows via parent_id.

    Rows whose parent is absent from the set become roots (so subtree/scoped queries still nest).
    Children are ordered by ``seq`` then ``path``. ``mask`` toggles the redaction projection.
    """
    projected: dict[str, dict[str, Any]] = {}
    for row in rows:
        node = _project_node(row, mask=mask)
        node["children"] = []
        projected[str(row["id"])] = node

    roots: list[dict[str, Any]] = []
    for row in rows:
        nid = str(row["id"])
        parent_id = row.get("parent_id")
        parent = projected.get(str(parent_id)) if parent_id else None
        if parent is not None:
            parent["children"].append(projected[nid])
        else:
            roots.append(projected[nid])

    def _sort(node: dict[str, Any]) -> None:
        node["children"].sort(key=lambda c: (c.get("seq") or 0, c.get("path") or ""))
        for child in node["children"]:
            _sort(child)

    for r in roots:
        _sort(r)
    roots.sort(key=lambda c: (c.get("seq") or 0, c.get("path") or ""))
    return roots


def project_facts(rows: list[dict[str, Any]], *, mask: bool = False,
                  verified_only: bool = False) -> list[dict[str, Any]]:
    """Project merged-fact rows, deriving sensitivity (from the attribute key) and a 'verified'
    flag (high confidence + no conflict); optionally filter to verified and/or mask values."""
    out: list[dict[str, Any]] = []
    for row in rows:
        verified = float(row.get("confidence") or 0.0) >= 0.8 and not row.get("conflict")
        if verified_only and not verified:
            continue
        item = dict(row)
        item["verified"] = verified
        sensitivity = sensitivity_for_key(row.get("attribute_key"))
        item["sensitivity"] = sensitivity
        if mask and sensitivity in _MASKABLE:
            item["resolved_value"] = _redact(item.get("resolved_value"))
            item["masked"] = True
        out.append(item)
    return out


def sensitivity_for_key(attribute_key: str | None) -> str:
    """Derive a sensitivity bucket from a canonical attribute key namespace."""
    key = attribute_key or ""
    if key.startswith("id."):
        return SensitivityBucket.critical.value
    if key.startswith(("identity.", "address.", "income.", "account.")):
        return SensitivityBucket.high.value
    return SensitivityBucket.low.value


def project_nodes(rows: list[dict[str, Any]], *, mask: bool = False) -> list[dict[str, Any]]:
    """Project a flat list of knode rows (e.g. ranked search hits), preserving rank/score."""
    out: list[dict[str, Any]] = []
    for row in rows:
        node = _project_node(row, mask=mask)
        for extra in ("_rank", "_score"):
            if extra in row:
                node[extra] = row[extra]
        out.append(node)
    return out


def build_manifest(doc_row: dict[str, Any], knode_rows: list[dict[str, Any]],
                   arep_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Self-describing capabilities manifest for one document (what it knows / can answer)."""
    node_types = Counter(str(n.get("node_type")) for n in knode_rows)
    facts = [n for n in knode_rows if n.get("node_type") == "fact"]
    attribute_keys = sorted({n["attribute_key"] for n in facts if n.get("attribute_key")})
    verification = Counter(str(f.get("verification_status")) for f in facts)
    rep_types = Counter(str(r.get("rep_type")) for r in (arep_rows or []))
    return {
        "doc_id": str(doc_row.get("id")),
        "document_name": doc_row.get("document_name"),
        "doc_type": doc_row.get("doc_type"),
        "jurisdiction": doc_row.get("jurisdiction"),
        "page_count": doc_row.get("page_count"),
        "languages": (doc_row.get("lang_profile") or {}).get("dominant_lang"),
        "sensitivity": doc_row.get("sensitivity_bucket"),
        "gate_decision": doc_row.get("gate_decision"),
        "node_type_counts": dict(node_types),
        "attribute_keys": attribute_keys,
        "verification_status_counts": dict(verification),
        "accessibility_rep_counts": dict(rep_types),
        "answerable": bool(rep_types.get(RepType.hypothetical_q.value)),
        "searchable": True,
    }


def answerable_questions(arep_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The 'answerable-questions' index: hypothetical questions this document can answer."""
    return [
        {"question": r.get("rep_text"), "knode_id": str(r.get("knode_id")),
         "path": r.get("path"), "lang": r.get("rep_lang")}
        for r in arep_rows
        if r.get("rep_type") == RepType.hypothetical_q.value
    ]
