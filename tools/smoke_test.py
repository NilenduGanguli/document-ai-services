#!/usr/bin/env python3
"""End-to-end smoke test against a running stack.

Exercises every consumer-facing flow and asserts the hardened behaviours:
auth (401/403), async ingest via job polling, idempotency, content-hash no-op, pagination caps,
server-side masking, verified-only semantics, provenance, the change cursor, readiness/metrics,
and admin erasure.

    docker compose up --build -d
    python tools/smoke_test.py                     # defaults to http://localhost:8080

Exits non-zero on the first failure. Writes a markdown report to reports/.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

BASE = os.environ.get("DI_BASE_URL", "http://localhost:8080")
API_KEY = os.environ.get("DI_API_KEY", "di_local_dev_key_change_me")
CLIENT = os.environ.get("DI_SMOKE_CLIENT", "SMOKE-CLIENT-001")

_results: list[tuple[str, bool, str]] = []
_failed = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global _failed
    _results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        _failed += 1
    return ok


def hdr(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {"X-API-KEY": API_KEY}
    h.update(extra or {})
    return h


def sample_pdf() -> bytes:
    """A tiny digital PDF with an MRZ-ish passport payload (text layer, no OCR needed)."""
    try:
        from fpdf import FPDF
    except ImportError:
        return b""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in [
        "PASSPORT / PASAPORTE",
        "United States of America",
        "Surname: SMITH",
        "Given Names: JOHN ROBERT",
        "Passport No: 123456789",
        "Nationality: USA",
        "Date of Birth: 12 MAR 1985",
        "Date of Issue: 01 JAN 2020",
        "Date of Expiry: 01 JAN 2030",
        "P<USASMITH<<JOHN<ROBERT<<<<<<<<<<<<<<<<<<<<<",
        "1234567890USA8503129M3001017<<<<<<<<<<<<<<02",
    ]:
        pdf.cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def wait_for_ready(client: httpx.Client, timeout: float = 90.0) -> dict[str, Any]:
    """Poll /readyz until the service reports ready (or the timeout expires)."""
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            r = client.get(f"{BASE}/readyz", timeout=5)
            last = r.json()
            if r.status_code == 200 and last.get("ready"):
                return last
        except httpx.HTTPError:
            pass
        time.sleep(2)
    return last


def poll_job(client: httpx.Client, job_id: str, timeout: float = 180.0) -> dict[str, Any]:
    """Poll a job to a terminal state, mirroring what the console does."""
    deadline = time.time() + timeout
    job: dict[str, Any] = {}
    while time.time() < deadline:
        r = client.get(f"{BASE}/api/v1/jobs/{job_id}", params={"client_id": CLIENT},
                       headers=hdr(), timeout=10)
        r.raise_for_status()
        job = r.json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.7)
    return job


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="skip the purge at the end")
    args = ap.parse_args()

    client = httpx.Client(follow_redirects=True)
    print(f"\nDocument Intelligence smoke test → {BASE}  (client_id={CLIENT})\n")

    # ---------------------------------------------------------------- readiness
    print("readiness")
    ready = wait_for_ready(client)
    check("/readyz reports ready", bool(ready.get("ready")),
          f"degraded={ready.get('degraded')}")
    comps = ready.get("components", {})
    for name in ("db", "migrations", "posture", "rls", "pgvector", "retrieval", "blob", "ocr",
                 "auth", "audit"):
        c = comps.get(name, {})
        check(f"  component {name}", bool(c.get("ok")),
              f"{c.get('detail','')} {c.get('extra','')}".strip())
    r = client.get(f"{BASE}/health", timeout=5)
    check("/health is liveness-only 200", r.status_code == 200)
    r = client.get(f"{BASE}/metrics", timeout=5)
    check("/metrics exposes prometheus text", r.status_code == 200 and "di_" in r.text,
          f"{len(r.text)} bytes")

    # ---------------------------------------------------------------- auth
    print("\nauth")
    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/facts", timeout=10)
    check("no API key -> 401", r.status_code == 401, f"got {r.status_code}")
    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/facts",
                   headers={"X-API-KEY": "totally-wrong"}, timeout=10)
    check("bad API key -> 401", r.status_code == 401, f"got {r.status_code}")
    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/facts", headers=hdr(), timeout=10)
    check("valid API key -> 200", r.status_code == 200, f"got {r.status_code}")

    # ---------------------------------------------------------------- ingest (async job)
    print("\ningest (202 + job)")
    pdf = sample_pdf()
    if not pdf:
        check("fpdf2 available to build a fixture", False, "pip install fpdf2")
        return 1
    files = {"file": ("passport.pdf", io.BytesIO(pdf), "application/pdf")}
    data = {"client_id": CLIENT, "external_document_id": "EXT-PASSPORT-1",
            "idempotency_key": "smoke-key-1"}
    r = client.post(f"{BASE}/api/v1/ingest", data=data, files=files, headers=hdr(), timeout=30)
    check("POST /ingest -> 202 + job_id", r.status_code == 202 and "job_id" in r.json(),
          f"got {r.status_code} {r.text[:120]}")
    job_id = r.json()["job_id"]

    job = poll_job(client, job_id)
    check("job reached succeeded", job.get("status") == "succeeded",
          f"status={job.get('status')} error={job.get('error')}")
    stages = [e["stage"] for e in job.get("events", [])]
    for stage in ("ocr", "gate", "extract", "subtree", "merge", "done"):
        check(f"  stage recorded: {stage}", stage in stages)
    done = next((e for e in job.get("events", []) if e["stage"] == "done"), {})
    detail = done.get("detail", {})
    doc_id = detail.get("doc_id") or job.get("doc_id")
    check("job carries doc_id", bool(doc_id), str(doc_id))
    check("blob retained via configured backend", bool(detail.get("blob_backend")),
          f"backend={detail.get('blob_backend')}")

    # idempotency
    files = {"file": ("passport.pdf", io.BytesIO(pdf), "application/pdf")}
    r = client.post(f"{BASE}/api/v1/ingest", data=data, files=files, headers=hdr(), timeout=30)
    check("same idempotency_key reuses the job", r.json().get("reused") is True,
          f"job_id={r.json().get('job_id')}")

    # content-hash no-op (new idempotency key, identical bytes)
    files = {"file": ("passport.pdf", io.BytesIO(pdf), "application/pdf")}
    data2 = {**data, "idempotency_key": "smoke-key-2"}
    r = client.post(f"{BASE}/api/v1/ingest", data=data2, files=files, headers=hdr(), timeout=30)
    job2 = poll_job(client, r.json()["job_id"])
    noop = any(e["stage"] == "version" and e.get("status") == "skip"
               for e in job2.get("events", []))
    check("identical re-upload no-ops (hash before OCR)", noop,
          f"stages={[e['stage'] for e in job2.get('events', [])]}")

    # ---------------------------------------------------------------- reads
    print("\nreads")
    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/documents", headers=hdr(), timeout=10)
    docs = r.json()
    check("GET /documents", r.status_code == 200 and docs["count"] >= 1,
          f"count={docs.get('count')}")
    first = docs["documents"][0] if docs.get("documents") else {}
    check("  document list omits raw OCR text", "ocr_text" not in first)
    check("  external_document_id round-tripped",
          first.get("external_document_id") == "EXT-PASSPORT-1",
          str(first.get("external_document_id")))
    check("  classified doc_type", bool(first.get("doc_type")), str(first.get("doc_type")))

    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/tree", headers=hdr(), timeout=15)
    tree = r.json()
    check("GET /tree", r.status_code == 200 and tree["count"] > 0, f"nodes={tree.get('count')}")
    check("  masked by default (server policy)", tree.get("masked") is True)

    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/facts", headers=hdr(), timeout=10)
    facts = r.json()
    check("GET /facts", r.status_code == 200, f"count={facts.get('count')}")
    sensitive = [f for f in facts.get("facts", [])
                 if f.get("sensitivity") in ("HIGH", "CRITICAL")]
    if sensitive:
        check("  sensitive values redacted by default",
              all(f.get("masked") for f in sensitive),
              f"{len(sensitive)} sensitive facts")
    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/facts", params={"mask": "false"},
                   headers=hdr(), timeout=10)
    check("  mask=false returns cleartext", r.json().get("masked") is False)

    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/facts", params={"verified_only": "true"},
                   headers=hdr(), timeout=10)
    vf = r.json().get("facts", [])
    check("verified_only excludes self-scored LLM facts",
          all(f.get("verification_status") != "llm_unverified" for f in vf),
          f"{len(vf)} verified facts")

    # search
    r = client.post(f"{BASE}/api/v1/clients/{CLIENT}/search",
                    json={"query": "passport number", "top_k": 5}, headers=hdr(), timeout=30)
    sr = r.json()
    check("POST /search", r.status_code == 200, f"hits={sr.get('count')} vector={sr.get('vector')}")
    r = client.post(f"{BASE}/api/v1/clients/{CLIENT}/search",
                    json={"query": "x", "top_k": 9999}, headers=hdr(), timeout=15)
    check("top_k above the cap is rejected", r.status_code == 422, f"got {r.status_code}")

    # provenance
    nodes = tree.get("tree", [])
    node_id = None

    def _first_id(items: list[dict[str, Any]]) -> str | None:
        for n in items:
            if n.get("id"):
                return str(n["id"])
            got = _first_id(n.get("children") or [])
            if got:
                return got
        return None

    node_id = _first_id(nodes)
    if node_id:
        r = client.get(f"{BASE}/api/v1/nodes/{node_id}/provenance",
                       params={"client_id": CLIENT}, headers=hdr(), timeout=10)
        p = r.json()
        check("GET /nodes/{id}/provenance", r.status_code == 200 and p.get("provenance") is not None,
              f"extractor={(p.get('provenance') or {}).get('extractor')}")

    # changes feed
    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/changes", headers=hdr(), timeout=10)
    ch = r.json()
    check("GET /changes", r.status_code == 200 and ch["count"] >= 1, f"count={ch.get('count')}")
    check("  change feed exposes a monotonic cursor", ch.get("next_seq") is not None,
          f"next_seq={ch.get('next_seq')}")
    if ch.get("next_seq"):
        r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/changes",
                       params={"after_seq": ch["next_seq"]}, headers=hdr(), timeout=10)
        check("  after_seq cursor drains to empty", r.json().get("count") == 0)

    # manifest / answerable
    if doc_id:
        r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/docs/{doc_id}/manifest",
                       headers=hdr(), timeout=10)
        check("GET /manifest", r.status_code == 200)
        r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/docs/{doc_id}/answerable",
                       headers=hdr(), timeout=10)
        check("GET /answerable", r.status_code == 200)

    # jobs list
    r = client.get(f"{BASE}/api/v1/jobs", params={"client_id": CLIENT, "limit": 1},
                   headers=hdr(), timeout=10)
    jl = r.json()
    check("GET /jobs paginates", r.status_code == 200 and len(jl.get("jobs", [])) == 1,
          f"next_cursor={'set' if jl.get('next_cursor') else 'none'}")

    # ---------------------------------------------------------------- auth hardening (Phase 2)
    print("\nauth hardening")

    # key rotation: mint a throwaway key, rotate it, prove the successor is live and the
    # predecessor is now time-boxed (not immediately dead — an overlap window).
    r = client.post(f"{BASE}/api/v1/admin/keys", headers=hdr(),
                    json={"name": "smoke-rotate", "client_ids": [CLIENT], "scopes": ["read"]},
                    timeout=10)
    check("POST /admin/keys creates a key", r.status_code == 201 and "api_key" in r.json(),
          f"got {r.status_code}")
    old_key_id = r.json().get("key_id")
    r = client.post(f"{BASE}/api/v1/admin/keys/{old_key_id}/rotate", headers=hdr(), json={},
                    timeout=10)
    rot = r.json()
    check("POST /admin/keys/{id}/rotate mints a successor", r.status_code == 200
          and "api_key" in rot and "old_key_expires_at" in rot, f"got {r.status_code} {rot}")
    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/facts",
                   headers={"X-API-KEY": rot.get("api_key", "")}, timeout=10)
    check("  rotated (successor) key authenticates", r.status_code == 200, f"got {r.status_code}")
    r = client.get(f"{BASE}/api/v1/admin/keys", headers=hdr(), timeout=10)
    listed = next((k for k in r.json() if k["id"] == rot.get("key_id")), None)
    check("  successor key lists with rotated_from set",
          bool(listed) and listed.get("rotated_from") == old_key_id, str(listed))

    # per-tenant ingest quota: an explicit daily_ingest_limit=0 override blocks the tenant
    # entirely — a deliberate admin lever, distinct from the fleet default's 0 (= unlimited).
    quota_client = f"{CLIENT}-QUOTA"
    r = client.put(f"{BASE}/api/v1/admin/tenants/{quota_client}/policy", headers=hdr(),
                   json={"daily_ingest_limit": 0}, timeout=10)
    check("PUT /admin/tenants/{id}/policy sets an override", r.status_code == 200
          and r.json().get("daily_ingest_limit") == 0, f"got {r.status_code} {r.text[:120]}")
    files = {"file": ("passport.pdf", io.BytesIO(pdf), "application/pdf")}
    data_q = {"client_id": quota_client, "external_document_id": "EXT-QUOTA-1",
              "idempotency_key": "smoke-quota-1"}
    r = client.post(f"{BASE}/api/v1/ingest", data=data_q, files=files, headers=hdr(), timeout=30)
    check("  blocked tenant's ingest -> 429", r.status_code == 429, f"got {r.status_code}")
    r = client.put(f"{BASE}/api/v1/admin/tenants/{quota_client}/policy", headers=hdr(), json={},
                   timeout=10)
    check("  clearing the policy override succeeds", r.status_code == 200, f"got {r.status_code}")

    # read-side access audit: the requests made throughout this run should show up for CLIENT.
    r = client.get(f"{BASE}/api/v1/admin/access-log", params={"client_id": CLIENT, "limit": 5},
                   headers=hdr(), timeout=10)
    al = r.json()
    check("GET /admin/access-log", r.status_code == 200 and al.get("count", 0) > 0,
          f"count={al.get('count')}")

    # ---------------------------------------------------------------- admin
    print("\nadmin / lifecycle")
    r = client.post(f"{BASE}/api/v1/admin/clients/{CLIENT}/adjudicate",
                    json={"attribute_key": "identity.family_name", "verdict": "override",
                          "value_text": "SMITH-CORRECTED", "reviewer": "smoke"},
                    headers=hdr(), timeout=20)
    check("POST /admin adjudicate", r.status_code == 200, r.text[:100])
    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/facts",
                   params={"attribute_key": "identity.family_name", "mask": "false"},
                   headers=hdr(), timeout=10)
    got = r.json().get("facts", [])
    if got:
        check("  adjudication overrides the merged value",
              got[0].get("resolved_value") == "SMITH-CORRECTED"
              and got[0].get("adjudicated") is True,
              f"value={got[0].get('resolved_value')}")

    # multi-valued-facts adjudication lifecycle (Phase 3): live verdict list, the append-only
    # history distinct from it, and clearing a verdict to revert to automatic resolution. Exercised
    # against the single-valued override above — no deterministic extractor produces a
    # multi-valued attribute (director/beneficial-owner) from the sample fixtures yet, so this
    # checks the adjudication CRUD lifecycle itself rather than fabricating instance data.
    r = client.get(f"{BASE}/api/v1/admin/clients/{CLIENT}/adjudications", headers=hdr(),
                   timeout=10)
    adjs = r.json() if r.status_code == 200 else []
    check("GET /admin adjudications lists the live verdict", r.status_code == 200 and any(
        a.get("attribute_key") == "identity.family_name" and a.get("verdict") == "override"
        for a in adjs), f"got {r.status_code}")

    r = client.get(f"{BASE}/api/v1/admin/clients/{CLIENT}/adjudications/history",
                   params={"attribute_key": "identity.family_name"}, headers=hdr(), timeout=10)
    history = r.json() if r.status_code == 200 else []
    check("GET /admin adjudications/history records the verdict", r.status_code == 200 and any(
        e.get("verdict") == "override" for e in history), f"got {r.status_code}")

    r = client.delete(f"{BASE}/api/v1/admin/clients/{CLIENT}/adjudications/identity.family_name",
                      headers=hdr(), timeout=20)
    clear = r.json() if r.status_code == 200 else {}
    check("DELETE /admin adjudications clears the verdict", r.status_code == 200
          and clear.get("cleared") is True, f"got {r.status_code} {r.text[:100]}")
    r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/facts",
                   params={"attribute_key": "identity.family_name", "mask": "false"},
                   headers=hdr(), timeout=10)
    reverted = r.json().get("facts", [])
    if reverted:
        check("  clearing the verdict reverts to automatic resolution",
              reverted[0].get("resolved_value") != "SMITH-CORRECTED"
              and reverted[0].get("adjudicated") is False,
              f"value={reverted[0].get('resolved_value')}")

    if not args.keep:
        r = client.post(f"{BASE}/api/v1/admin/clients/{CLIENT}/purge",
                        json={"confirm_client_id": CLIENT}, headers=hdr(), timeout=60)
        check("POST /admin purge (right-to-erasure)", r.status_code == 200,
              str(r.json().get("deleted"))[:120])
        r = client.get(f"{BASE}/api/v1/clients/{CLIENT}/documents", headers=hdr(), timeout=10)
        check("  tenant data is gone", r.json().get("count") == 0)

    # ---------------------------------------------------------------- rate limit (runs LAST:
    # deliberately exhausts the bootstrap key's token bucket, which would 429-starve every check
    # above if it ran earlier)
    print("\nrate limit")

    def _one_get() -> httpx.Response:
        return httpx.get(f"{BASE}/api/v1/clients/{CLIENT}/facts", headers=hdr(), timeout=10)

    with ThreadPoolExecutor(max_workers=32) as pool:
        burst = list(pool.map(lambda _: _one_get(), range(200)))
    throttled = [r for r in burst if r.status_code == 429]
    check("concurrent burst trips the rate limiter", len(throttled) > 0,
          f"{len(throttled)}/{len(burst)} got 429")
    if throttled:
        check("  429 carries Retry-After", "retry-after" in throttled[0].headers,
              dict(throttled[0].headers))

    # ---------------------------------------------------------------- report
    total = len(_results)
    passed = total - _failed
    print(f"\n{'=' * 60}\n{passed}/{total} checks passed" +
          (f" — {_failed} FAILED" if _failed else " — all green") + f"\n{'=' * 60}\n")

    out = Path("reports/smoke-test-report.md")
    out.parent.mkdir(exist_ok=True)
    lines = [
        "# Smoke test report", "",
        f"- Target: `{BASE}`", f"- Client: `{CLIENT}`",
        f"- Result: **{passed}/{total} passed**" + (f", {_failed} failed" if _failed else ""),
        "", "| Check | Result | Detail |", "|---|---|---|",
    ]
    lines += [f"| {n} | {'PASS' if ok else 'FAIL'} | {d} |" for n, ok, d in _results]
    out.write_text("\n".join(lines) + "\n")
    print(f"report → {out}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
