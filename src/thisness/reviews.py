"""The review queue (design 04 §2).

A review asks a person a question consensus would not answer automatically. This
module reads a review with its context from stored records, and turns an answer into
one change set: merges and promotions inside each confirmed group, relationships
when asked for, pair constraints so the question is never asked again, and the
review's closure, which releases its holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations, pairwise
from typing import TYPE_CHECKING, Any

from thisness.core.decisions import (
    Actor,
    ConsensusDecision,
    PairJudgment,
    ReviewDecision,
    ReviewResolution,
    Verdict,
)
from thisness.core.review import ReviewItem
from thisness.core.run import (
    AddConstraint,
    ChangeSet,
    CloseReview,
    Merge,
    Operation,
    Promote,
    ReadSet,
    Relate,
    Split,
)
from thisness.storage.errors import InvalidOperation
from thisness.storage.protocol import CommitResult

if TYPE_CHECKING:
    from thisness.engine import Thisness

QUESTIONS = {
    "cluster": "Are these one entity, or several? Group them.",
    "boundary": "Are these the same entity, different entities, or related ones?",
    "merge": "Should these entities be merged?",
    "split": "Should this entity be split?",
}


@dataclass(frozen=True)
class ReviewSummary:
    review: ReviewItem
    question: str
    observations: int  # observations whose identity the answer can change
    largest_entity: int


@dataclass(frozen=True)
class NodeView:
    node_id: str
    kind: str  # observation | entity
    entity_id: str | None
    entity_state: str | None
    size: int  # observations in the node's entity
    attributes: dict[str, Any]
    source: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReviewCase:
    review: ReviewItem
    question: str
    decision: ConsensusDecision | None
    nodes: tuple[NodeView, ...]
    judgments: tuple[PairJudgment, ...]
    suggested_cut: tuple[tuple[str, ...], tuple[str, ...]] | None


def _is_observation(node: str) -> bool:
    return node.startswith("obs_")


def _entities_of_nodes(engine: Thisness, nodes: Sequence[str]) -> dict[str, str]:
    observations = [n for n in nodes if _is_observation(n)]
    out = dict(engine.store.entities_of(observations))
    out.update({n: n for n in nodes if not _is_observation(n)})
    return out


def list_reviews(engine: Thisness, kind: str | None = None) -> list[ReviewSummary]:
    reviews = [r for r in engine.store.open_reviews() if kind is None or r.kind == kind]
    nodes = sorted({n for r in reviews for n in r.members})
    entity_of = _entities_of_nodes(engine, nodes)
    sizes = {e: len(m) for e, m in engine.store.members_of(sorted(set(entity_of.values()))).items()}
    summaries = []
    for review in reviews:
        entities = {entity_of[n] for n in review.members if n in entity_of}
        summaries.append(
            ReviewSummary(
                review=review,
                question=QUESTIONS[review.kind],
                observations=sum(sizes.get(e, 0) for e in entities),
                largest_entity=max((sizes.get(e, 0) for e in entities), default=0),
            )
        )
    summaries.sort(key=lambda s: (-s.observations, -s.largest_entity, s.review.opened_seq))
    return summaries


def build_case(engine: Thisness, review_id: str) -> ReviewCase:
    store = engine.store
    review = store.review(review_id)
    opening = store.decisions([review.decision_id])
    decision = opening[0] if opening and isinstance(opening[0], ConsensusDecision) else None
    entity_of = _entities_of_nodes(engine, review.members)
    entities = sorted(set(entity_of.values()))
    states = store.entity_states(entities)
    members = store.members_of(entities)
    observations = {o.observation_id: o for o in store.observations(
        [n for n in review.members if _is_observation(n)])}  # fmt: skip
    nodes = []
    for node in review.members:
        entity = entity_of.get(node)
        if _is_observation(node):
            obs = observations[node]
            attributes = dict(obs.attributes or {})
            source = obs.source.model_dump(mode="json")
        else:
            try:
                attributes = dict(store.snapshot(node).attributes)
            except Exception:  # an entity without a snapshot (retired): show nothing
                attributes = {}
            source = None
        nodes.append(
            NodeView(
                node_id=node,
                kind="observation" if _is_observation(node) else "entity",
                entity_id=entity,
                entity_state=states.get(entity) if entity else None,
                size=len(members.get(entity, ())) if entity else 0,
                attributes=attributes,
                source=source,
            )
        )
    involved = sorted({o for e in entities for o in members.get(e, ())}
                      | {n for n in review.members if _is_observation(n)})  # fmt: skip
    judgments = tuple(store.pair_judgments(involved, run_id=review.run_id))
    cut = None
    if decision is not None and decision.suggested_cut is not None:
        cut = (decision.suggested_cut.keep, decision.suggested_cut.move)
    return ReviewCase(review, QUESTIONS[review.kind], decision, tuple(nodes), judgments, cut)


def _verdict(resolution: ReviewResolution, groups: Sequence[Sequence[str]]) -> Verdict:
    if resolution == "dismiss":
        return Verdict.REQUIRES_REVIEW
    if len(groups) == 1:
        return Verdict.MATCH
    if all(len(g) == 1 for g in groups):
        return Verdict.NO_MATCH
    return Verdict.POSSIBLE_MATCH


def resolve(
    engine: Thisness,
    review_id: str,
    resolution: ReviewResolution,
    *,
    groups: Sequence[Sequence[str]] | None = None,
    relation: str | None = None,
    reason: str,
    actor: Actor | None = None,
    recommendation_id: str | None = None,
) -> CommitResult:
    """Answer a review (design 04 §2.4). ``groups`` partitions its members when the
    resolution is ``groups``, splitting entities a group divides; ``same``/``approve`` mean
    one group, ``different``/``reject`` one group per current entity; ``related`` relates the two entities involved."""
    store, ids = engine.store, engine.ids
    review = store.review(review_id)
    if review.status != "open":
        raise InvalidOperation(-1, f"review {review_id} is {review.status}")
    members = list(review.members)
    if resolution == "groups":
        if groups is None or sorted(n for g in groups for n in g) != sorted(members):
            raise InvalidOperation(-1, "groups must partition the review's members exactly")
        partition = [sorted(g) for g in groups]
    entity_of = _entities_of_nodes(engine, members)
    if resolution in ("same", "approve"):
        partition = [sorted(members)]
    elif resolution in ("different", "reject", "related"):
        # One group per current entity: members that already share an entity stay together.
        by_entity: dict[str, list[str]] = {}
        for m in sorted(members):
            by_entity.setdefault(entity_of.get(m, m), []).append(m)
        partition = list(by_entity.values())
    elif resolution != "groups":
        partition = []
    entities = sorted(set(entity_of.values()))
    states = store.entity_states(entities)
    sizes = {e: len(m) for e, m in store.members_of(entities).items()}
    first_member = {e: m[0] for e, m in store.members_of(entities).items() if m}

    def anchor(node: str) -> str | None:
        return node if _is_observation(node) else first_member.get(node)

    ops: list[Operation] = []
    decision = ReviewDecision(
        decision_id=ids.decision(),
        actor=actor or Actor(kind="human", id="reviewer"),
        review_id=review_id,
        verdict=_verdict(resolution, partition),
        resolution=resolution,
        groups=tuple(tuple(g) for g in partition) if partition else None,
        relation=relation if resolution == "related" else None,
        reason=reason,
        recommendation_id=recommendation_id,
        created_at=engine.clock(),
    )
    survivors: list[str] = []
    if resolution != "dismiss":
        # A group may take only some of an entity's members: the entity stays with the
        # group that holds it (its own node, else most of its members) and the rest are
        # split out into new entities, which then join their groups' merges.
        placed: dict[str, list[list[str]]] = {}
        for index, group in enumerate(partition):
            for node in group:
                if node in entity_of:
                    placed.setdefault(entity_of[node], [[] for _ in partition])[index].append(node)
        home: dict[str, int] = {}
        for entity, by_group in placed.items():
            own = [i for i, g in enumerate(by_group) if entity in g]
            home[entity] = own[0] if own else max(
                range(len(partition)), key=lambda i: (len(by_group[i]), -i))  # fmt: skip
        group_entities: list[set[str]] = [set() for _ in partition]
        fresh: set[str] = set()
        for entity, by_group in sorted(placed.items()):
            group_entities[home[entity]].add(entity)
            targets = {}
            for index, nodes in enumerate(by_group):
                if index != home[entity] and nodes:
                    target = ids.entity(entity.split("_", 1)[0])
                    targets[target] = tuple(sorted(nodes))
                    group_entities[index].add(target)
                    states[target], sizes[target] = states.get(entity), len(nodes)
                    fresh.add(target)
            if targets:
                ops.append(Split(source_id=entity, targets=targets,
                                 decision_id=decision.decision_id))  # fmt: skip
                sizes[entity] = sizes.get(entity, 0) - sum(map(len, targets.values()))
        for group in group_entities:
            ranked = sorted(group, key=lambda e: (states.get(e) != "active", e in fresh,
                                                  -sizes.get(e, 0), e))  # fmt: skip
            if not ranked:
                continue
            survivor, *others = ranked
            if others:
                ops.append(Merge(survivor_id=survivor, merged_ids=tuple(others),
                                 decision_id=decision.decision_id))  # fmt: skip
            if states.get(survivor) == "provisional":
                ops.append(Promote(entity_id=survivor, decision_id=decision.decision_id))
            survivors.append(survivor)
        member_anchors = [a for a in map(anchor, members) if a is not None]
        existing = {(c.kind, c.left_id, c.right_id) for c in store.constraints(member_anchors)}

        def link(kind: str, a: str | None, b: str | None) -> None:
            if a is None or b is None or a == b:
                return
            left, right = min(a, b), max(a, b)
            if (kind, left, right) in existing:
                return
            existing.add((kind, left, right))
            ops.append(AddConstraint(constraint_id=ids.constraint(), kind=kind, left_id=left,
                                     right_id=right, decision_id=decision.decision_id))  # fmt: skip

        anchors = [sorted({a for a in map(anchor, g) if a is not None}) for g in partition]
        for group_anchors in anchors:
            for a, b in pairwise(group_anchors):
                link("must_link", a, b)
        for first, second in combinations([a for a in anchors if a], 2):
            link("cannot_link", first[0], second[0])
        if resolution == "related":
            distinct = sorted(set(survivors))
            if len(distinct) != 2:
                raise InvalidOperation(-1, "a relationship needs exactly two entities")
            ops.append(Relate(relationship_id=ids.relationship(), relation=relation,
                              subject_entity_id=distinct[0], object_entity_id=distinct[1],
                              decision_id=decision.decision_id))  # fmt: skip
    status = "dismissed" if resolution == "dismiss" else "resolved"
    ops.append(CloseReview(review_id=review_id, status=status, decision_id=decision.decision_id))
    store.record_decisions([decision])
    changes = ChangeSet(base_seq=store.head_seq(), ops=tuple(ops),
                        read_set=ReadSet(entity_ids=frozenset(entities)))  # fmt: skip
    return store.commit(changes, actor=decision.actor, canonicalizer=engine.canonicalizer)
