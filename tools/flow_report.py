"""Exercise every Document AI Services API flow against a running instance and write a report.

Usage:
    DI_BASE_URL=http://localhost:8080 python tools/flow_report.py

Uploads the plain-text sample documents in ``samples/`` (driven through the OCR text passthrough),
streams the ingest SSE stages, then runs every read/search flow — capturing the request and the
response for each — into ``reports/local-flow-test-report.md``.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import httpx

BASE = os.environ.get("DI_BASE_URL", "http://localhost:8080").rstrip("/")
CLIENT = os.environ.get("DI_DEMO_CLIENT", "acme-bank-001")
ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
OUT = ROOT / "reports" / "local-flow-test-report.md"

SAMPLE_FILES = [
    "passport_specimen.txt",
    "us_ssn_card.txt",
    "mx_ine_credencial.txt",
    "us_utility_bill.txt",
]

md: list[str] = []


def w(s: str = "") -> None:
    md.append(s)


def prune(obj, maxlen: int = 300):
    """Truncate long string values so the report stays readable."""
    if isinstance(obj, str):
        return obj if len(obj) <= maxlen else obj[:maxlen] + f"… (+{len(obj) - maxlen} chars)"
    if isinstance(obj, list):
        return [prune(x, maxlen) for x in obj]
    if isinstance(obj, dict):
        return {k: prune(v, maxlen) for k, v in obj.items()}
    return obj


def jblock(obj, maxlen: int = 300) -> None:
    w("```json")
    w(json.dumps(prune(obj, maxlen), indent=2, default=str))
    w("```")


def ingest(path: pathlib.Path) -> list[dict]:
    files = {"file": (path.name, path.read_bytes(), "text/plain")}
    data = {"client_id": CLIENT}
    events: list[dict] = []
    with httpx.Client(timeout=180) as c:
        with c.stream("POST", f"{BASE}/api/v1/ingest", data=data, files=files) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                line = line.strip()
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
    return events


def get(path: str, **params):
    with httpx.Client(timeout=60) as c:
        r = c.get(f"{BASE}{path}", params=params)
        return r.status_code, r.json()


def post(path: str, body: dict):
    with httpx.Client(timeout=60) as c:
        r = c.post(f"{BASE}{path}", json=body)
        return r.status_code, r.json()


def main() -> int:
    # health
    with httpx.Client(timeout=10) as c:
        health = c.get(f"{BASE}/health").json()

    w("# Document AI Services — Local Flow Test Report")
    w("")
    w(f"- **Target:** `{BASE}`  ·  **Client:** `{CLIENT}`")
    w(f"- **Service health:** `{json.dumps(health)}`")
    w("- Documents are uploaded as `text/plain` and pass through the OCR text-passthrough, so the "
      "full pipeline (gate → extraction → subtree → merge → persist) runs without a live OCR engine.")
    w("- Model gateway runs in offline **stub** mode unless `RETRIEVAL_BASE_URL` is set.")
    w("")

    ingest_results: dict[str, dict] = {}

    w("## 1. Ingestion (per document)")
    for fname in SAMPLE_FILES:
        path = SAMPLES / fname
        w(f"\n### `{fname}`")
        w("\n**Input (uploaded text):**")
        w("```")
        w(path.read_text().strip())
        w("```")
        try:
            events = ingest(path)
        except Exception as e:  # noqa: BLE001
            w(f"\n**ERROR:** `{e}`")
            continue
        w("\n**SSE stage events (output):**")
        jblock(events, maxlen=200)
        done = next((e for e in events if e.get("stage") == "done"), {})
        ingest_results[fname] = done.get("detail", {})

    # collect a doc_id and a fact node id for later flows
    docs_status, docs = get(f"/api/v1/clients/{CLIENT}/documents")

    w("\n## 2. Documents inventory")
    w(f"`GET /api/v1/clients/{CLIENT}/documents` → {docs_status}")
    jblock(docs)

    # full tree (unmasked) for the MX INE doc (rich fact set)
    ine_doc_id = next((d["id"] for d in docs.get("documents", [])
                       if d.get("doc_type") == "MX_INE"), None)

    w("\n## 3. Knowledge tree — unmasked vs masked (toggleable projection)")
    if ine_doc_id:
        st, tree = get(f"/api/v1/clients/{CLIENT}/tree", doc_id=ine_doc_id, mask="false")
        w(f"`GET /clients/{CLIENT}/tree?doc_id={ine_doc_id}&mask=false` → {st}")
        jblock(tree, maxlen=160)
        st, tree_m = get(f"/api/v1/clients/{CLIENT}/tree", doc_id=ine_doc_id, mask="true")
        w(f"\n`GET …&mask=true` → {st}  (sensitive values redacted; structure preserved)")
        jblock(tree_m, maxlen=160)

    w("\n## 4. Merged client-level facts (cross-document)")
    st, facts = get(f"/api/v1/clients/{CLIENT}/facts")
    w(f"`GET /clients/{CLIENT}/facts` → {st}")
    jblock(facts)
    st, facts_v = get(f"/api/v1/clients/{CLIENT}/facts", verified_only="true", mask="true")
    w(f"\n`GET …?verified_only=true&mask=true` → {st}")
    jblock(facts_v)

    w("\n## 5. Hybrid search (scoped to client; dense+lexical+structural)")
    for q in ["passport number", "curp date of birth", "electric account"]:
        st, res = post(f"/api/v1/clients/{CLIENT}/search", {"query": q, "top_k": 3})
        w(f"\n`POST /clients/{CLIENT}/search` body=`{{'query': '{q}', 'top_k': 3}}` → {st}")
        jblock(res, maxlen=160)

    # manifest + answerable for the utility bill (SEND_TO_LLM -> has accessibility reps)
    bill_doc_id = next((d["id"] for d in docs.get("documents", [])
                        if d.get("doc_type") == "UTILITY_BILL"), None)
    w("\n## 6. Capabilities manifest + answerable-questions (self-describing)")
    target_doc = bill_doc_id or ine_doc_id
    if target_doc:
        st, manifest = get(f"/api/v1/clients/{CLIENT}/docs/{target_doc}/manifest")
        w(f"`GET /clients/{CLIENT}/docs/{target_doc}/manifest` → {st}")
        jblock(manifest)
        st, ans = get(f"/api/v1/clients/{CLIENT}/docs/{target_doc}/answerable")
        w(f"\n`GET …/answerable` → {st}")
        jblock(ans, maxlen=200)

    # provenance for one fact node
    w("\n## 7. Node provenance (grounding)")
    if ine_doc_id:
        _, tree = get(f"/api/v1/clients/{CLIENT}/tree", doc_id=ine_doc_id)

        def _find_fact(nodes):
            for n in nodes:
                if n.get("node_type") == "fact":
                    return n
                found = _find_fact(n.get("children", []))
                if found:
                    return found
            return None

        fact_node = _find_fact(tree.get("tree", []))
        if fact_node:
            nid = fact_node["id"]
            st, prov = get(f"/api/v1/nodes/{nid}/provenance", client_id=CLIENT)
            w(f"`GET /nodes/{nid}/provenance?client_id={CLIENT}` → {st}")
            jblock(prov)

    w("\n## 8. Version delta feed")
    st, changes = get(f"/api/v1/clients/{CLIENT}/changes")
    w(f"`GET /clients/{CLIENT}/changes` → {st}")
    jblock(changes, maxlen=160)

    w("\n## 9. Idempotent re-upload (versioning no-op on identical content)")
    events = ingest(SAMPLES / "us_ssn_card.txt")
    w("Re-uploading `us_ssn_card.txt` (unchanged) — SSE:")
    jblock(events, maxlen=160)

    OUT.write_text("\n".join(md) + "\n")
    print(f"report written to {OUT}")
    print(f"ingested: {list(ingest_results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
