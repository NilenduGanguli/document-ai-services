# KYC Classification & Extraction — Design for Review

**Status:** proposed, not implemented. This document exists to be argued with.
**Date:** 2026-08-06 · **Scope:** India + USA + Canada + Mexico
**Research basis:** 5 parallel research tracks (fleet audit, classification techniques,
extraction techniques, KYC document inventory, throughput architecture). Every claim about
existing code was read from source; every claim about the outside world is cited in the
research appendix.

---

## 0. The headline, before anything else

Two findings should shape your reading:

**1. Your proposed approach is right, and it is already ~60% architected in DAS.**
`di/ontology.py` already declares, per document type, exactly the pairing you described —
the classification keyword map *and* the extraction field list, together:

```python
DocTypeSpec("US_W2", "US Form W-2", "income", ("US",),
    anchors_en=("W-2", "WAGE AND TAX STATEMENT", "OMB No. 1545-0008"),
    id_patterns=(r"\d{2}-\d{7}",), deterministic=True,
    attribute_keys=("id.ein", "identity.full_name", ...))
```

`di/gate/anchors.py` already implements the density intuition with specificity weighting,
and `di/gate/routing.py` is already the PII chokepoint that stops unclassified content
reaching an LLM. **This is not a greenfield build. It is a completion.**

**2. The existing classifier does not work, and would have silently failed in production.**
Measured against the live module, not inferred:

| Input | Result |
|---|---|
| A real US passport data page (`PASSPORT`, `TYPE P`, `P<USA…`) | `PASSPORT` @ **0.5335** — **below the 0.55 floor**, so it is rejected as UNKNOWN |
| Prose with no KYC content: *"the mi**dl**e of the b**ein**g of our business u**sin**g … **Sat**urday"* | 5 confident-looking candidates: `UTILITY_BILL` 0.50, `BANK_STATEMENT` 0.22, `US_DRIVER_LICENSE` 0.099, `US_EIN_LETTER` 0.099, `CA_SIN` 0.099 |

Cause: substring `in` matching with no word boundaries (`DL` ⊂ "mi**dl**e", `EIN` ⊂
"b**ein**g", `SIN` ⊂ "u**sin**g", `USA` ⊂ "ca**usa**", `SAT` ⊂ "**Sat**urday"), no IDF, no
length normalisation, no runner-up margin, no abstain band, and a confidence curve
`1 − 0.5^score` that caps a single perfect anchor at exactly 0.50. There is also **no
trained model shipped** — `DocTypeClassifier` always falls through to anchors.

So this work is not additive polish. It fixes a component that currently mis-classifies
both directions.

---

## 1. Where this should live — the one decision I want you to make first

You described classification/extraction as *"a parallel thing that uses the similar
services"*. The research recommends against a standalone service, and I agree, with a
caveat.

**Recommendation: a new package `di/fields/` + a hardened `di/gate/` inside DAS, exposed
over HTTP so DES and retrieval can call it.**

Why not a separate service:

- It needs `di/ontology.py` (the doctype/attribute vocabulary), `di/models.py` (KNode /
  fact / verification_status / provenance), the PII gate, and the merge + adjudication
  spine. Extracting those into a shared library is a bigger, riskier change than the
  feature itself.
- **Two classifiers in one fleet will disagree about what a document is.** DAS already
  classifies during ingest. A second, separate classifier creates a correctness problem
  (which one wins? what happens when they differ?) that no amount of engineering removes.
- Facts must land in `knode` + `client_merged_fact` to be usable — that is in DAS.

The caveat that preserves your intent: build it as a **self-contained package behind a
narrow interface** (`classify(LayoutView) -> Classification`,
`extract(LayoutView, DocSchema) -> ExtractedField[]`), with a `LayoutView` adapter over
DES's `OcrResult`. It gets HTTP endpoints so it *is* callable as a service by DES and
retrieval, and it can be lifted out later without rewriting the core. DES gains only one
thing: `features=keyValuePairs` on the Layout call.

**Open question for you:** if you want a physically separate deployable regardless, say so
— it is achievable, it costs a shared-library extraction and a rule about who owns
classification truth, and I would want that decision explicit rather than assumed.

---

## 2. Classification — a precision-first cascade

The principle: **KYC documents are designed to announce what they are, in large type, at
the top of page one.** Your intuition is correct. It needs IDF, saturation, length
normalisation and calibration bolted on, plus a layer of near-certain evidence above it.

```
INPUT: Azure prebuilt-layout JSON (already fetched and persisted by DES)
   │  normalise → raw / case-folded / accent-skeleton, zone-tagged by paragraph.role + bbox
   │
   ├─ L0  structural prior          0.1 ms   page count, aspect ratio, selection marks,
   │                                          table count, scanned-vs-digital
   ├─ L1  ANCHOR + CHECKSUM         1–3 ms   p = 0.95–0.99   ← near-certain
   │        MRZ TD1/TD2/TD3 (7-3-1 check digits)
   │        stdnum: in_.aadhaar/pan/epic/gstin · mx.curp/rfc · ca.sin · us.ssn/ein
   │        decisive title-zone anchors (rapidfuzz partial_ratio ≥ 90 on the skeleton)
   ├─ L2  ZONE-WEIGHTED BM25        1–3 ms   calibrated p̂    ← the "keyword density" tier
   │        log-odds / informative-Dirichlet term profiles, top-60 terms per class per lang
   │        k₁=1.2, b=0.75; zone weights γ_title=3.0 … γ_furniture=0.25
   │        + coverage + structural prior → robust-z → softmax(T=0.6) → Platt calibration
   ├─ L3  EMBEDDING kNN             2–8 ms   k=5 cosine over stored doctype exemplars
   ├─ L4  AZURE CUSTOM CLASSIFIER   1–3 s    optional, per-tenant, explicitly gated
   └─ L5  UNKNOWN → operator queue + periodic clustering to mine new doctypes
             (never auto-forwards to an LLM)

FUSION  score_c = logP(c | structure) + 3.0·anchor + 1.0·lexical + 0.8·knn
ACCEPT  p̂ ≥ θ_c  AND  margin over runner-up ≥ 0.25  AND  term coverage ≥ 0.20
```

Four properties worth calling out:

- **Word-boundary tokenisation is non-negotiable** — it alone removes the `DL`/`EIN`/`SIN`
  false-positive class demonstrated above.
- **Zone weighting is what makes this better than grep.** A term in a `title` paragraph is
  worth 3.0×; the same term in `pageFooter` furniture is worth 0.25×. This costs nothing
  because DES already paid Azure for the roles.
- **Per-page classification is free packet-splitting.** KYC arrives as merged PDFs.
  Page-level scoring plus run-length aggregation gives document boundaries without a second
  vendor call.
- **Abstain is a first-class outcome.** UNKNOWN routes to a human queue, never to an LLM.
  Classification becomes a *hard precondition* for the existing gate, rather than a
  co-equal stage.

---

## 3. Extraction — tiered, cheapest-first, LLM last

```
[doc_type + confidence] ──gate──┐
 DES layout artifacts (paragraphs / tables / selection marks / KV pairs / spans)
        │
   SCHEMA REGISTRY: DocSchema(doc_type, version) → FieldSpec[]
        │
 T1  LOCAL RESOLVER      pure, in-process, p95 < 50 ms, $0
       per field, run locators and score: label-anchored (right/below within a bbox
       window), table-cell addressing, selection-mark binding, positional/template
       anchoring, regex/checksum sweep, MRZ decode
       ├─ all required fields ≥ accept threshold → DONE (straight-through)
       └─ gaps ↓
 T2  AZURE SPECIALIST    2nd analyze call, only where a prebuilt model covers the doctype
       prebuilt-idDocument | tax.us.* | bankStatement.us | payStub.us
       └─ gaps ↓
 T3  AZURE queryFields   ≤20 named fields — long-tail / no-schema documents only
       └─ gaps ↓
 T4  LLM CONSTRAINED EXTRACT   only if the gate says SEND_TO_LLM / REDACT_THEN_SEND
       window-scoped · JSON-Schema-constrained · span-grounded · answer must be
       reconstructible from the cited span or it is rejected
       └─ gaps or low confidence ↓
 T5  HITL QUEUE — field-level items with an evidence pane; blind double-entry for CRITICAL
        │
 VALIDATORS → CROSS-FIELD RULES → ExtractedField[] → fact knodes → existing merge
```

The design intent you articulated — *"based on the document structure and layout we know
where to look"* — is T1, and T1 should answer the large majority of fields at zero
marginal cost. The LLM is a fallback for the long tail, not the mechanism.

**Schema management.** `DocSchema(doc_type, version)` with per-field name, type,
cardinality, required flag, label aliases (per language), validator, and locator hints.
Versioned and additive-only, so a schema change never silently reinterprets stored facts.

**Auto-generated schemas for unseen doctypes** (your requirement): induce candidate fields
from a sample of documents — recurring labels across the set, Layout key-value pairs, and
table headers — cluster and name them, then require a human to confirm before the schema
goes live. Induction proposes; it does not activate.

---

## 4. Document coverage — 116 doctypes vs the current 18

| Country | Proposed | Today | Notable additions |
|---|---:|---:|---|
| **India** | **36** | **0** | Aadhaar (+ masked variant), PAN, EPIC/Voter ID, DL, Passport, Form 60, CKYC record, NREGA job card, NPR letter, GST certificate, CIN/MOA/AOA, LLP, partnership deed, ITR-V, Form 16, bank passbook/statement, cancelled cheque, utility bills, rent agreement, board resolution |
| **USA** | 35 | 5 | Green card, EAD, birth/naturalisation/citizenship certs, ITIN letter, 147C, CP-575, Articles of Incorporation/Organization, operating agreement, bylaws, certificate of good standing, FinCEN BOIR + certificate, 1040, mortgage/lease |
| **Canada** | 25 | 5 | PR card, COPR, citizenship cert, refugee protection doc, secure status card, health card, NEXUS, provincial photo ID, CRA NOA, T1, BN letter, articles (federal/provincial), certificate of status, trust deed |
| **Mexico** | 20 | 5 | Matrícula consular, tarjeta de residente, acta de nacimiento, cédula profesional, cartilla militar, e.firma certificate, opinión de cumplimiento, predial, CFE/Telmex/agua comprobantes, poder notarial |

India is the whole missing quadrant, and it brings requirements the current code cannot
meet: **Devanagari and other Indic scripts** (the anchor matcher registers only `en`/`es`),
and **Aadhaar masking obligations under UIDAI rules** — which is a handling constraint on
storage and display, not just an extraction detail.

Each doctype carries: stable `doctype_id`, issuing authority, category, classification
anchors (including bilingual variants), the confusable set with its disambiguating term,
and the field list with validators.

**Validators verified by execution**, not assumed: Aadhaar (Verhoeff), PAN (structure +
entity/surname character positions), CURP (18-char check digit), RFC, SSN, SIN (Luhn), EIN
prefix rules, GSTIN, and ICAO 9303 MRZ (7-3-1).

---

## 5. Throughput

The two optimisations worth more than everything else combined:

1. **Content-hash Layout cache** keyed on `(sha256, model_id, api_version, feature_set)`.
   A cache hit is ~30 ms and $0 against ~6 s and a per-page charge. The feature set must be
   in the key, or changing `azure_di_features` silently serves a KV-less payload to a
   KV-dependent extractor.
2. **Reuse DES's stored artifacts.** DES already persists the full `analyzeResult` per page
   and the chunk embeddings. Classification and extraction should read those, never
   re-OCR.

Everything else — reusing DAS's durable queue (`di_job`, `FOR UPDATE SKIP LOCKED`, leases,
per-tenant caps, dead-letter), batching embeddings, per-tenant fairness against the Azure
quota — is already-proven machinery in this fleet.

**Cache scope needs your decision.** Per-tenant is trivially safe. A *cross-tenant* cache
is where the money is, because blank standard forms (W-9, W-8BEN, Form 60, CKYC forms,
FATCA declarations, bank AOFs) are a large share of KYC volume and contain no PII. But a
global cache leaks *existence* — "this exact byte sequence has been seen before" — which is
a real side channel in a per-client KYC platform. **Proposed:** per-tenant by default;
cross-tenant only for an allowlist of blank-form/template documents that extracted zero
fields, behind an explicit config flag.

---

## 6. What I am not certain about

These should be closed before implementation locks, not guessed at in code:

| # | Unknown | How to close it |
|---|---|---|
| U1 | What `prebuilt-idDocument` actually returns for **Aadhaar and PAN** — no India-specific fields are in the published schema | Run ~10 of each through Azure Studio; capture the JSON |
| U2 | The exact `keyValuePairs[]` shape, and whether each pair carries a confidence | One live Layout call with `features=keyValuePairs` |
| U3 | **KV-pair recall on ID cards vs forms** — expected to be a form feature with poor card recall. This is an expectation, not a measurement | Measure on a 50-doc mixed sample |
| U4 | **Azure DI region availability and data residency for India (DPDP Act) and Mexico**, and whether content logging can be disabled | Azure region matrix + DI Transparency Note + your DPA |
| U5 | `stdnum.us.itin` omits the IRS-valid 50–65 group range — verified by reading its source | Must be patched or wrapped, or legitimate ITINs get rejected |
| U6 | `stdnum.in_.epic` enforces a Luhn digit that genuine EPICs reportedly fail | **Do not hard-reject on EPIC checksum.** Confidence booster only |
| U7 | Indian DL number has **no national standard and no checksum** | Heuristic regex only; never use as an identity/dedup key |

---

## 7. Proposed build order

Each phase is independently useful and independently verifiable.

| Phase | Deliverable | Why first |
|---|---|---|
| **0** | Fix the classifier's correctness bugs: word-boundary tokenisation, margin + abstain, recalibrate the confidence curve. Golden set from existing samples. | The current one mis-classifies in both directions. Everything downstream inherits this. |
| **1** | `DocSchema` registry + `LayoutView` adapter over DES's `OcrResult`; DES adds `features=keyValuePairs` | The substrate both classification and extraction need |
| **2** | L1+L2 cascade (anchors/checksums + zone-weighted BM25) with calibration | Should answer 90%+ of volume at ~$0 |
| **3** | T1 local resolver (label-anchored, table-cell, selection-mark, positional, regex/MRZ locators) + validators | The extraction workhorse |
| **4** | India doctype pack (36) incl. Indic-script anchors + Aadhaar masking handling | The missing quadrant |
| **5** | Remaining NA doctype packs to 116; confusable negative-anchor rules | Coverage |
| **6** | T2/T3 Azure specialists, T4 constrained LLM, T5 HITL queue | Long tail |
| **7** | L3 embedding kNN, L4 optional Azure classifier, auto-schema induction | Refinement |

---

## 8. Questions for you

1. **Package inside DAS (recommended) or a physically separate service?** §1.
2. **Cross-tenant Layout cache** for blank forms — worth the existence side channel? §5.
3. **Is India in scope for phase 1**, or is North America first and India a follow-on?
4. **HITL** — do you have a review UI/queue today, or does T5 need building?
5. **Aadhaar masking**: store masked-only, or store full with masked projection at read
   time (DAS's existing `mask_by_default` pattern would support the latter)?

Nothing here is implemented. On your sign-off — including any of the above you want changed
— I will start at Phase 0.
