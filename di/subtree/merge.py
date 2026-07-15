"""Client-level confidence-weighted fact merge.

Across a client's documents the same canonical attribute (e.g. ``identity.date_of_birth``) may be
asserted by several sources — an MRZ read, an anchored KV, an LLM extraction. This module collapses
those into one resolved :class:`~di.models.ClientFact` per ``attribute_key``.

Resolution policy (pure, deterministic, no DB / no network):

* Group inputs by ``attribute_key``.
* The resolved value is the one carried by the **highest-confidence** source — recency is
  intentionally *not* a tiebreaker here (the subtree's bitemporal columns own validity windows).
* ``conflict`` (and therefore ``needs_review``) is set when contributing sources disagree on the
  comparable value for that key.
* ``source_fact_ids`` lists every contributing ``fact_id`` (winners and losers alike).
* ``confidence`` is the winning source's confidence.

The module is pure: it imports only :mod:`di.models` and the stdlib, so it loads without any heavy
optional dependency.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from di.models import ClientFact, VerificationStatus


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
    verdict: str                       # accept | reject | override
    value_text: str | None = None
    value_date: date | None = None
    value_num: float | None = None
    reviewer: str | None = None
    note: str | None = None


def _comparable_key(f: FactInput) -> tuple[str | None, str | None, float | None]:
    """A normalized, hashable view of a fact's value used to detect agreement/conflict.

    String values are compared case-insensitively and whitespace-trimmed so that cosmetic OCR
    differences ("John  Smith" vs "john smith") are not flagged as conflicts.
    """
    norm_value: str | None = None
    if f.value is not None:
        collapsed = " ".join(f.value.split())
        norm_value = collapsed.casefold() or None
    iso_date = f.value_date.isoformat() if f.value_date is not None else None
    return (norm_value, iso_date, f.value_num)


def _is_empty(comparable: tuple[str | None, str | None, float | None]) -> bool:
    return all(part is None for part in comparable)


def merge_facts(facts: list[FactInput], client_id: str = "",
                adjudications: dict[str, Adjudication] | None = None,
                ontology_version: str | None = None) -> list[ClientFact]:
    """Collapse candidate facts into one resolved :class:`ClientFact` per attribute key.

    Args:
        facts: Candidate facts (any number, any mix of attribute keys).
        client_id: Optional client identifier stamped onto every resulting fact.
        adjudications: Human decisions keyed by ``attribute_key``. These are applied *after*
            automatic resolution and win outright, so a reviewer's correction is not silently
            clobbered by the next ingest.
        ontology_version: Stamped onto each fact so its vintage survives ontology changes.

    Returns:
        One :class:`ClientFact` per distinct ``attribute_key``, sorted by ``attribute_key`` for a
        stable, deterministic ordering.
    """
    adjudications = adjudications or {}
    grouped: dict[str, list[FactInput]] = {}
    for f in facts:
        grouped.setdefault(f.attribute_key, []).append(f)

    out: list[ClientFact] = []
    for attribute_key in sorted(grouped):
        group = grouped[attribute_key]

        # Winner: highest confidence. Ties broken by input order (first one wins) for determinism.
        winner = max(group, key=lambda f: f.confidence)

        # Conflict: distinct, non-empty comparable values disagree across contributing sources.
        distinct_values = {
            _comparable_key(f) for f in group if not _is_empty(_comparable_key(f))
        }
        conflict = len(distinct_values) > 1

        fact = ClientFact(
            client_id=client_id,
            attribute_key=attribute_key,
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
        _apply_adjudication(fact, adjudications.get(attribute_key))
        out.append(fact)
    return out


def _apply_adjudication(fact: ClientFact, adj: Adjudication | None) -> None:
    """Overlay a human decision onto an automatically-resolved fact (in place)."""
    if adj is None:
        return
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
        # Reviewed and rejected: keep it visible but explicitly unresolved.
        fact.resolved_value = None
        fact.value_date = None
        fact.value_num = None
        fact.verification_status = VerificationStatus.unverified
        fact.confidence = 0.0
        fact.needs_review = True


__all__ = ["Adjudication", "FactInput", "merge_facts"]
