from __future__ import annotations

import pytest

from thisness.benchmark.evaluate import run_benchmark
from thisness.benchmark.organizations import generate
from thisness.storage import InvalidOperation
from thisness.storage.duckdb.invariants import check


@pytest.fixture
def engine():
    _, engine, _ = run_benchmark(generate(n_orgs=300, seed=11), random_pairs=60_000)
    yield engine
    assert check(engine.store) == []


def first(engine, kind, *, entities=None):
    for summary in engine.reviews(kind):
        case = engine.review_case(summary.review.review_id)
        if entities is None or len({n.entity_id for n in case.nodes}) == entities:
            return case
    pytest.skip(f"no {kind} review with {entities} entities in this benchmark")


def test_reviews_are_ordered_by_impact(engine):
    summaries = engine.reviews()
    assert summaries
    keys = [(s.observations, s.largest_entity) for s in summaries]
    assert keys == sorted(keys, reverse=True)


def test_a_case_carries_everything_a_reviewer_needs(engine):
    case = first(engine, "boundary")
    assert {n.node_id for n in case.nodes} == set(case.review.members)
    assert case.question.startswith("Are these the same entity")
    assert case.judgments and all(j.decision_type == "pair" for j in case.judgments)
    assert all(n.entity_id is not None for n in case.nodes)


def test_different_persists_as_cannot_links_and_releases_holds(engine):
    case = first(engine, "boundary", entities=2)
    review_id = case.review.review_id
    entities = sorted({n.entity_id for n in case.nodes})
    assert set(case.review.members) <= engine._held_nodes()
    engine.resolve_review(review_id, "different", reason="separate companies")
    assert engine.store.review(review_id).status == "resolved"
    assert not set(case.review.members) & engine._held_nodes()
    # The answer is durable: these two can no longer be merged by anyone, until revoked.
    with pytest.raises(InvalidOperation, match="revoke it first"):
        engine.merge_entities(entities[0], [entities[1]], reason="try anyway")
    constraint = next(c for c in engine.constraints() if c.kind == "cannot_link")
    engine.revoke_constraint(constraint.constraint_id, reason="new information")
    engine.merge_entities(entities[0], [entities[1]], reason="now allowed")


def test_same_merges_and_links(engine):
    case = first(engine, "boundary")
    entities = sorted({n.entity_id for n in case.nodes})
    engine.resolve_review(case.review.review_id, "same", reason="one company")
    survivors = {e for e in entities if engine.store.entity(e).state != "retired"}
    assert len(survivors) == 1
    survivor = survivors.pop()
    assert engine.store.entity(survivor).state == "active"
    assert all(engine.store.entity_of(n.node_id) == survivor
               for n in case.nodes if n.kind == "observation")  # fmt: skip
    assert any(c.kind == "must_link" for c in engine.constraints())


def test_related_creates_a_relationship(engine):
    case = first(engine, "boundary", entities=2)
    a, b = sorted({n.entity_id for n in case.nodes})
    engine.resolve_review(case.review.review_id, "related", relation="subsidiary_of",
                          reason="annual report")  # fmt: skip
    assert [r.relation for r in engine.store.relationships(a)] == ["subsidiary_of"]
    assert engine.store.entity(a).state == engine.store.entity(b).state == "active"


def test_groups_partition_a_cluster_review(engine):
    case = first(engine, "cluster")
    members = sorted(case.review.members)
    groups = [members[:1], members[1:]] if len(members) > 1 else [members]
    engine.resolve_review(case.review.review_id, "groups", groups=groups, reason="split")
    homes = []
    for group in groups:
        entities = {engine.store.entity_of(n) if n.startswith("obs_") else n for n in group}
        assert len(entities) == 1
        homes.append(entities.pop())
        assert engine.store.entity(homes[-1]).state == "active"
    # The first member shared an entity with another; answering split it out.
    assert len(set(homes)) == len(groups)


def test_dismiss_changes_nothing_but_the_review(engine):
    case = first(engine, "boundary")
    before = len(engine.constraints())
    engine.resolve_review(case.review.review_id, "dismiss", reason="not now")
    assert engine.store.review(case.review.review_id).status == "dismissed"
    assert len(engine.constraints()) == before


def test_bad_answers_are_refused(engine):
    case = first(engine, "boundary")
    review_id = case.review.review_id
    with pytest.raises(InvalidOperation, match="partition"):
        engine.resolve_review(review_id, "groups", groups=[list(case.review.members)[:1]],
                              reason="incomplete")  # fmt: skip
    engine.resolve_review(review_id, "dismiss", reason="done")
    with pytest.raises(InvalidOperation, match="dismissed"):
        engine.resolve_review(review_id, "same", reason="again")
