"""Relationship candidates (design 04 §2.6).

Pairs of observations whose comparisons fell on a relationship level (an Organization
``name`` of ``brand_only``: same brand, different company) or matched a relationship
pattern (a Person household: same family name, same address) suggest that their
entities are related, not the same. They are derived from stored evidence on
demand, never stored as review items, so they cannot bury the identity queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from thisness.engine import Thisness

MAX_EVIDENCE = 20
LLM_RELATED = ("llm_adjudication", "related")  # an adjudicator's answer (design 05 §4.4)


@dataclass(frozen=True)
class RelationshipCandidate:
    left_entity: str
    right_entity: str
    signals: tuple[str, ...]  # "comparison/level", or a pattern's name
    pairs: int  # observation pairs carrying a signal
    evidence_ids: tuple[str, ...]  # the first MAX_EVIDENCE
    neighborhood: str  # the smallest entity id among the entities linked by signals
    neighborhood_size: int


MAX_NEIGHBORHOOD = 30  # entities in one neighborhood of relationship candidates


def relationship_candidates(
    engine: Thisness, entity_id: str | None = None, entity_type: str | None = None
) -> list[RelationshipCandidate]:
    """Entity pairs linked by relationship signals and not yet related, grouped by
    neighborhood (largest first). With ``entity_id``, only the pairs that include it."""
    store = engine.store
    profiles = [engine.profiles[entity_type]] if entity_type else list(engine.profiles.values())
    levels = sorted({level for p in profiles for level in p.relationship_level_pairs}
                    | {LLM_RELATED})  # fmt: skip
    rows = store.relationship_signals(levels)
    held: dict[tuple[str, str], dict[str, str]] = {}
    evidence_of: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        pair = (r["left_id"], r["right_id"])
        held.setdefault(pair, {})[r["attribute"]] = r["level"]
        evidence_of.setdefault(pair, []).append(r["evidence_id"])
    nodes = sorted({o for pair in held for o in pair})
    entity_of = store.entities_of([n for n in nodes if n.startswith("obs_")])
    # An adjudicator judges node pairs, so its evidence can name entities directly.
    entity_of.update({n: n for n in nodes if not n.startswith("obs_")})
    signals: dict[tuple[str, str], set[str]] = {}
    pairs: dict[tuple[str, str], set[tuple[str, str]]] = {}
    evidence: dict[tuple[str, str], list[str]] = {}
    for (left, right), levels_held in held.items():
        found = {s for p in profiles for s in p.relationship_signals(levels_held)}
        if levels_held.get(LLM_RELATED[0]) == LLM_RELATED[1]:
            found.add("model_said_related")
        a, b = entity_of.get(left), entity_of.get(right)
        if not found or a is None or b is None or a == b:
            continue
        key = (a, b) if a < b else (b, a)
        signals.setdefault(key, set()).update(found)
        pairs.setdefault(key, set()).add((left, right))
        evidence.setdefault(key, []).extend(evidence_of[(left, right)])
    entities = sorted({e for key in signals for e in key})
    for rel in store.relationships_among(entities):
        a, b = sorted((rel.subject_entity_id, rel.object_entity_id))
        signals.pop((a, b), None)

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # Neighborhoods are built from the best-supported pairs first, at most
    # MAX_NEIGHBORHOOD entities each: on real UK companies, unbounded, relationship
    # signals chained 14,255 entities (80% of the store) into one (design 07 §13.4). A
    # pair joining two full neighborhoods is still a candidate, listed in its left one.
    grown: dict[str, int] = {}
    for a, b in sorted(signals, key=lambda k: (-len(pairs[k]), k)):
        ra, rb = find(a), find(b)
        if ra == rb or grown.get(ra, 1) + grown.get(rb, 1) > MAX_NEIGHBORHOOD:
            continue
        keep, other = min(ra, rb), max(ra, rb)
        parent[other] = keep
        grown[keep] = grown.get(ra, 1) + grown.get(rb, 1)
    size: dict[str, int] = {}
    for e in {e for key in signals for e in key}:
        size[find(e)] = size.get(find(e), 0) + 1
    out = [
        RelationshipCandidate(
            left_entity=a,
            right_entity=b,
            signals=tuple(sorted(signals[(a, b)])),
            pairs=len(pairs[(a, b)]),
            evidence_ids=tuple(sorted(evidence[(a, b)])[:MAX_EVIDENCE]),
            neighborhood=find(a),
            neighborhood_size=size[find(a)],
        )
        for a, b in signals
        if entity_id is None or entity_id in (a, b)
    ]
    out.sort(key=lambda c: (-c.neighborhood_size, c.neighborhood, c.left_entity, c.right_entity))
    return out
