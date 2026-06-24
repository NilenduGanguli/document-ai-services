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

from di.models import ClientFact


class FactInput(BaseModel):
    """One candidate fact feeding the merge. Mirrors the comparable bits of an extracted field."""

    fact_id: str
    attribute_key: str
    value: str | None = None
    value_date: date | None = None
    value_num: float | None = None
    confidence: float = 0.0


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


def merge_facts(facts: list[FactInput], client_id: str = "") -> list[ClientFact]:
    """Collapse candidate facts into one resolved :class:`ClientFact` per attribute key.

    Args:
        facts: Candidate facts (any number, any mix of attribute keys).
        client_id: Optional client identifier stamped onto every resulting fact.

    Returns:
        One :class:`ClientFact` per distinct ``attribute_key``, sorted by ``attribute_key`` for a
        stable, deterministic ordering.
    """
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

        out.append(
            ClientFact(
                client_id=client_id,
                attribute_key=attribute_key,
                resolved_value=winner.value,
                value_date=winner.value_date,
                value_num=winner.value_num,
                confidence=winner.confidence,
                conflict=conflict,
                needs_review=conflict,
                source_fact_ids=[f.fact_id for f in group],
            )
        )
    return out


__all__ = ["FactInput", "merge_facts"]
