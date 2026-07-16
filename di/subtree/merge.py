"""Client-level confidence-weighted fact merge.

Across a client's documents the same canonical attribute (e.g. ``identity.date_of_birth``) may be
asserted by several sources — an MRZ read, an anchored KV, an LLM extraction. This module collapses
those into resolved :class:`~di.models.ClientFact` rows, one per ``attribute_key`` for
single-valued attributes and one per detected *instance* (director, beneficial owner, account...)
for attributes declared multi-valued in ``di.ontology.MULTI_VALUED_ATTRIBUTE_KEYS``.

Resolution policy (pure, deterministic, no DB / no network):

* Group inputs by ``attribute_key``, then — for multi-valued keys only — sub-group by a
  deterministic value-fingerprint (:func:`instance_fingerprint`) so several concurrent instances
  (three directors, say) coexist instead of collapsing into one false "conflict".
* Within each group (or sub-group), the resolved value is the one carried by the
  **highest-confidence** source — recency is intentionally *not* a tiebreaker here (the subtree's
  bitemporal columns own validity windows).
* ``conflict`` (and therefore ``needs_review``) is set when contributing sources disagree on the
  comparable value within that group/sub-group.
* ``source_fact_ids`` lists every contributing ``fact_id`` (winners and losers alike).
* ``confidence`` is the winning source's confidence.

The module is pure: it imports only :mod:`di.models` and the stdlib, so it loads without any heavy
optional dependency.
"""
from __future__ import annotations

import hashlib
import hmac
import unicodedata
from datetime import date

from pydantic import BaseModel

from di.models import ClientFact, VerificationStatus

#: Version tag for the identity-normalization function below. Immutable once shipped — a smarter
#: matcher (e.g. entity resolution) ships as a new algo string with an explicit re-adjudication
#: plan, never by silently changing what this one means for already-computed fingerprints.
IDENTITY_ALGO = "nfkd-casefold-ws-v1"


class FactInput(BaseModel):
    """One candidate fact feeding the merge. Mirrors the comparable bits of an extracted field."""

    fact_id: str
    attribute_key: str
    value: str | None = None
    value_date: date | None = None
    value_num: float | None = None
    confidence: float = 0.0
    #: How the source was verified (checksum/registry vs self-scored LLM). Carried to the winner so
    #: the serving layer can distinguish "verified" from "the model said 0.9".
    verification_status: VerificationStatus = VerificationStatus.unverified


class Adjudication(BaseModel):
    """A human decision that must survive every subsequent re-merge."""

    attribute_key: str
    #: '' for single-valued attributes; a specific instance_key for a multi-valued one. Sticky
    #: identity handle — an override does not re-derive this from the overridden value.
    instance_key: str = ""
    verdict: str                       # accept | reject | override
    value_text: str | None = None
    value_date: date | None = None
    value_num: float | None = None
    reviewer: str | None = None
    note: str | None = None


def _comparable_key(f: FactInput) -> tuple[str | None, str | None, float | None]:
    """A normalized, hashable view of a fact's value used to detect agreement/conflict.

    String values are compared case-insensitively and whitespace-trimmed so that cosmetic OCR
    differences ("John  Smith" vs "john smith") are not flagged as conflicts. Used for
    single-valued attributes; multi-valued instance sub-groups use :func:`_identity_comparable_key`
    instead so accent variants of the same identity do not reintroduce false conflicts.
    """
    norm_value: str | None = None
    if f.value is not None:
        collapsed = " ".join(f.value.split())
        norm_value = collapsed.casefold() or None
    iso_date = f.value_date.isoformat() if f.value_date is not None else None
    return (norm_value, iso_date, f.value_num)


def _is_empty(comparable: tuple[str | None, str | None, float | None]) -> bool:
    return all(part is None for part in comparable)


def _normalize_identity(s: str) -> str:
    """Accent-fold + casefold + whitespace-collapse (versioned as :data:`IDENTITY_ALGO`).

    Extends :func:`_comparable_key`'s normalization with accent folding (NFKD decompose, drop
    combining marks) so "Juan Pérez Gómez" and "JUAN PEREZ GOMEZ" are treated as the same identity
    — the common case across a scanned Acta Constitutiva (accented) and an OCR'd INE (often not).
    """
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def _identity_basis(f: FactInput) -> str | None:
    """The primary identity dimension for instance fingerprinting: text, else date, else number.
    None when the fact carries no comparable value at all — such candidates form no instance."""
    if f.value:
        return f.value
    if f.value_date is not None:
        return f.value_date.isoformat()
    if f.value_num is not None:
        return f"{f.value_num:.10g}"
    return None


def _identity_comparable_key(f: FactInput) -> tuple[str | None, str | None, float | None]:
    """Conflict-detection key for a multi-key instance sub-group.

    Uses the SAME normalization as the instance fingerprint (:func:`_normalize_identity`,
    accent-folding included), not :func:`_comparable_key`. Members of one sub-group already share
    a fingerprint (hence an identical normalized primary dimension) by construction, so this tuple
    only ever disagrees on ``value_date``/``value_num`` — e.g. two documents asserting different
    appointment dates for the same director. Using the coarser ``_comparable_key`` here instead
    would resurrect the exact false-conflict the fingerprint was built to eliminate: an accented
    and unaccented spelling of the same name would fingerprint into one instance yet permanently
    flag conflict=true via a text comparison the fingerprint doesn't share.
    """
    normalized = _normalize_identity(f.value) if f.value else None
    norm_value = normalized or None
    iso_date = f.value_date.isoformat() if f.value_date is not None else None
    return (norm_value, iso_date, f.value_num)


def instance_fingerprint(normalized: str, hmac_key: str = "") -> str:
    """Deterministic 64-bit (16 hex char) identity fingerprint for an already-normalized string.

    HMAC-SHA256 with a stable deployment-scoped key when one is configured (recommended for
    production — see ``Settings.instance_fingerprint_hmac_key`` and ``di.posture``): an unsalted
    hash lets anyone with API access dictionary-confirm a low-entropy value (a director's name)
    against a fully masked response, since the fingerprint rides through even when
    ``resolved_value`` is redacted. Falls back to plain SHA-256 when no key is configured — an
    accepted, documented risk for local/demo use; ``di.posture`` flags an unset key in production.
    """
    encoded = normalized.encode("utf-8")
    if hmac_key:
        digest = hmac.new(hmac_key.encode("utf-8"), encoded, hashlib.sha256).hexdigest()
    else:
        digest = hashlib.sha256(encoded).hexdigest()
    return digest[:16]


def _merge_group(attribute_key: str, group: list[FactInput], client_id: str,
                 ontology_version: str | None, instance_key: str, *,
                 comparable_key_fn=_comparable_key) -> ClientFact:
    """Resolve one group of candidate facts (a whole single-valued attribute, or one multi-valued
    instance sub-group) into a :class:`ClientFact`. Shared by both the single- and multi-valued
    paths so their resolution rule — max-confidence winner, conflict on disagreement — is
    identical; only the value-comparison function and the resulting ``instance_key`` differ.
    """
    # Winner: highest confidence. Ties broken by input order (first one wins) for determinism.
    winner = max(group, key=lambda f: f.confidence)

    # Conflict: distinct, non-empty comparable values disagree across contributing sources.
    distinct_values = {
        comparable_key_fn(f) for f in group if not _is_empty(comparable_key_fn(f))
    }
    conflict = len(distinct_values) > 1

    return ClientFact(
        client_id=client_id,
        attribute_key=attribute_key,
        instance_key=instance_key,
        resolved_value=winner.value,
        value_date=winner.value_date,
        value_num=winner.value_num,
        confidence=winner.confidence,
        conflict=conflict,
        needs_review=conflict,
        source_fact_ids=[f.fact_id for f in group],
        verification_status=winner.verification_status,
        winning_fact_id=winner.fact_id,
        ontology_version=ontology_version,
        resolution_rationale={
            "rule": "max_confidence",
            "candidates": len(group),
            "winner_confidence": winner.confidence,
            "distinct_values": len(distinct_values),
        },
    )


def _merge_multi_key(attribute_key: str, group: list[FactInput], client_id: str,
                     adjudications: dict[tuple[str, str], Adjudication],
                     ontology_version: str | None, hmac_key: str) -> list[ClientFact]:
    """Sub-group a multi-valued attribute's candidates by instance fingerprint, resolve each
    sub-group independently, and apply per-instance adjudication (reject removes the row)."""
    sub_groups: dict[str, list[FactInput]] = {}
    basis_by_key: dict[str, str] = {}
    for f in group:
        basis = _identity_basis(f)
        if basis is None:
            continue
        normalized = _normalize_identity(basis)
        if not normalized:
            continue
        key = instance_fingerprint(normalized, hmac_key)
        sub_groups.setdefault(key, []).append(f)
        basis_by_key[key] = normalized

    # "Siblings for this key" — cross-instance plurality is surfaced for reviewers (e.g. 7
    # near-duplicate spellings looks suspicious) but is explicitly NOT a conflict signal.
    instance_count = len(sub_groups)

    out: list[ClientFact] = []
    for instance_key, sub in sub_groups.items():
        fact = _merge_group(attribute_key, sub, client_id, ontology_version, instance_key,
                            comparable_key_fn=_identity_comparable_key)
        fact.resolution_rationale = {
            **fact.resolution_rationale,
            "instance_key": instance_key,
            "identity_basis": basis_by_key[instance_key],
            "identity_algo": IDENTITY_ALGO,
            "instance_count": instance_count,
        }
        adj = adjudications.get((attribute_key, instance_key))
        if _apply_adjudication(fact, adj, is_multi=True):
            out.append(fact)
    return out


def merge_facts(facts: list[FactInput], client_id: str = "",
                adjudications: dict[tuple[str, str], Adjudication] | None = None,
                ontology_version: str | None = None,
                multi_keys: frozenset[str] = frozenset(),
                fingerprint_hmac_key: str = "") -> list[ClientFact]:
    """Collapse candidate facts into resolved :class:`ClientFact` rows.

    Args:
        facts: Candidate facts (any number, any mix of attribute keys).
        client_id: Optional client identifier stamped onto every resulting fact.
        adjudications: Human decisions keyed by ``(attribute_key, instance_key)`` — ``instance_key``
            is ``''`` for single-valued attributes. Applied *after* automatic resolution and win
            outright, so a reviewer's correction is not silently clobbered by the next ingest.
        ontology_version: Stamped onto each fact so its vintage survives ontology changes.
        multi_keys: Attribute keys to treat as multi-valued (see
            ``di.ontology.MULTI_VALUED_ATTRIBUTE_KEYS``). Keys not listed here are single-valued —
            the default empty set reproduces the pre-multi-value behavior exactly, byte-identical,
            with every ``instance_key`` set to ``''``.
        fingerprint_hmac_key: Deployment-scoped key for :func:`instance_fingerprint`. Empty uses
            plain SHA-256 (accepted risk for local/demo use).

    Returns:
        One :class:`ClientFact` per distinct ``attribute_key`` (single-valued) or per detected
        instance (multi-valued), sorted by ``(attribute_key, instance_key)`` for a stable,
        deterministic ordering. A multi-valued instance that was adjudicated ``reject`` is omitted
        entirely; the durable record of that decision lives in the adjudication history, not here.
    """
    adjudications = adjudications or {}
    grouped: dict[str, list[FactInput]] = {}
    for f in facts:
        grouped.setdefault(f.attribute_key, []).append(f)

    out: list[ClientFact] = []
    for attribute_key in sorted(grouped):
        group = grouped[attribute_key]
        if attribute_key in multi_keys:
            out.extend(_merge_multi_key(attribute_key, group, client_id, adjudications,
                                        ontology_version, fingerprint_hmac_key))
        else:
            fact = _merge_group(attribute_key, group, client_id, ontology_version, "")
            if _apply_adjudication(fact, adjudications.get((attribute_key, "")), is_multi=False):
                out.append(fact)
    out.sort(key=lambda f: (f.attribute_key, f.instance_key))
    return out


def _apply_adjudication(fact: ClientFact, adj: Adjudication | None, *, is_multi: bool) -> bool:
    """Overlay a human decision onto an automatically-resolved fact (in place).

    Returns:
        ``False`` when the fact must be omitted from the output entirely — a reject on a
        multi-valued instance ("remove a spurious director"). A reject on a single-valued
        attribute keeps the row (legacy behavior: nulled and flagged), so it always returns
        ``True`` when ``is_multi`` is ``False``.
    """
    if adj is None:
        return True
    fact.adjudicated = True
    fact.resolution_rationale = {
        **fact.resolution_rationale,
        "adjudication": {"verdict": adj.verdict, "reviewer": adj.reviewer, "note": adj.note},
    }
    if adj.verdict == "override":
        fact.resolved_value = adj.value_text
        fact.value_date = adj.value_date
        fact.value_num = adj.value_num
        fact.verification_status = VerificationStatus.human_verified
        fact.confidence = 1.0
        fact.conflict = False
        fact.needs_review = False
    elif adj.verdict == "accept":
        # The automatic winner was reviewed and confirmed: clear the review flag, keep the value.
        fact.verification_status = VerificationStatus.human_verified
        fact.conflict = False
        fact.needs_review = False
    elif adj.verdict == "reject":
        if is_multi:
            return False
        # Single-valued: reviewed and rejected — keep it visible but explicitly unresolved.
        fact.resolved_value = None
        fact.value_date = None
        fact.value_num = None
        fact.verification_status = VerificationStatus.unverified
        fact.confidence = 0.0
        fact.needs_review = True
    return True


__all__ = ["Adjudication", "FactInput", "IDENTITY_ALGO", "instance_fingerprint", "merge_facts"]
