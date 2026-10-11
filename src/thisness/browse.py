"""Listing and search over identity state, for the Workbench and the CLI (design 04 §4).

Read-only. The Workbench calls these through ``Thisness`` and never queries the store
itself (invariant 13).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from thisness.core.observation import Observation
from thisness.storage.errors import NotFound

if TYPE_CHECKING:
    from thisness.engine import Thisness

PREFIXES = {"obs": "observation", "rev": "review", "dec": "decision", "run": "run",
            "con": "constraint", "rel": "relationship"}  # fmt: skip


@dataclass(frozen=True)
class EntityRow:
    entity_id: str
    state: str
    version: int
    name: str | None
    observations: int
    sources: int
    open_reviews: int


@dataclass(frozen=True)
class EntityPage:
    rows: tuple[EntityRow, ...]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True)
class SearchHit:
    kind: str  # entity | observation | review | decision | run | constraint | relationship
    id: str
    label: str
    detail: str


def _open_reviews_by_entity(engine: Thisness) -> Counter[str]:
    reviews = engine.store.open_reviews()
    observations = sorted({n for r in reviews for n in r.members if n.startswith("obs_")})
    entity_of = engine.store.entities_of(observations)
    counts: Counter[str] = Counter()
    for r in reviews:
        for e in {entity_of.get(n, n) for n in r.members if n in entity_of or n[:4] != "obs_"}:
            counts[e] += 1
    return counts


def entities(
    engine: Thisness,
    entity_type: str,
    *,
    state: str | None = None,
    query: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> EntityPage:
    rows, total = engine.store.entity_rows(entity_type, state=state, query=query,
                                           offset=offset, limit=limit)  # fmt: skip
    reviews = _open_reviews_by_entity(engine)
    return EntityPage(
        tuple(
            EntityRow(
                r["entity_id"],
                r["state"],
                r["version"],
                r["name"],
                r["observations"],
                r["sources"],
                reviews.get(r["entity_id"], 0),
            )
            for r in rows
        ),
        total,
        offset,
        limit,
    )


def overview(engine: Thisness) -> dict[str, Any]:
    store = engine.store
    return {
        "observations": store.observation_count(),
        "entities": store.entity_counts(),
        "open_reviews": len(store.open_reviews()),
        "head_seq": store.head_seq(),
        "models": {t: engine.model_source(t) for t in sorted(engine.profiles)},
    }


def observation(engine: Thisness, observation_id: str) -> Observation:
    found = engine.store.observations([observation_id])
    if not found:
        raise NotFound(f"observation {observation_id}")
    return found[0]


def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def search(engine: Thisness, q: str, limit: int = 8) -> list[SearchHit]:
    """Entities by name, or any record by its id."""
    q = q.strip()
    if not q:
        return []
    store = engine.store
    prefix = q.split("_", 1)[0]
    if "_" in q and prefix in PREFIXES:
        kind = PREFIXES[prefix]
        try:
            if kind == "observation":
                o = observation(engine, q)
                return [SearchHit(kind, q, str((o.attributes or {}).get("name", q)),
                                  f"{o.source.type}:{o.source.id}")]  # fmt: skip
            if kind == "review":
                r = store.review(q)
                return [SearchHit(kind, q, f"{r.kind} review", r.status)]
            if kind == "decision" and store.decisions([q]):
                return [SearchHit(kind, q, store.decisions([q])[0].decision_type, "decision")]
        except NotFound:
            return []
        return []
    hits: list[SearchHit] = []
    for entity_type in sorted(engine.profiles):
        page = entities(engine, entity_type, query=q, limit=limit)
        hits += [
            SearchHit(
                "entity",
                r.entity_id,
                r.name or r.entity_id,
                f"{entity_type}, {r.state}, {_n(r.observations, 'observation')}",
            )
            for r in page.rows
        ]
    return hits[:limit]
