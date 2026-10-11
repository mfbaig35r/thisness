from __future__ import annotations

import pytest

from thisness.benchmark.evaluate import run_benchmark
from thisness.benchmark.organizations import generate
from thisness.storage.duckdb.invariants import check


@pytest.fixture
def engine():
    _, engine, _ = run_benchmark(generate(n_orgs=300, seed=11), random_pairs=60_000)
    yield engine
    assert check(engine.store) == []


def test_candidates_are_grouped_by_neighborhood(engine):
    candidates = engine.relationship_candidates()
    assert candidates
    sizes = [c.neighborhood_size for c in candidates]
    assert sizes == sorted(sizes, reverse=True)
    for c in candidates:
        assert c.left_entity < c.right_entity
        # One brand, another business: whichever record pair supplies the signal reads as
        # a shared brand, another qualifier or added words (design 07 §13.2).
        assert c.signals and set(c.signals) <= {"name/brand_only", "name/qualifier_differs",
                                                "name/token_subset"}  # fmt: skip
        assert c.neighborhood <= c.left_entity
        assert engine.store.entity(c.left_entity).state != "retired"


def test_relating_a_candidate_removes_it(engine):
    first = engine.relationship_candidates()[0]
    mine = engine.relationship_candidates(first.left_entity)
    assert first in mine and all(first.left_entity in (c.left_entity, c.right_entity)
                                 for c in mine)  # fmt: skip
    engine.relate(first.left_entity, "sibling_of", first.right_entity, reason="same group",
                  evidence_ids=first.evidence_ids)  # fmt: skip
    pairs = {(c.left_entity, c.right_entity) for c in engine.relationship_candidates()}
    assert (first.left_entity, first.right_entity) not in pairs


def test_merging_a_candidate_pair_removes_it(engine):
    first = engine.relationship_candidates()[0]
    engine.merge_entities(first.left_entity, [first.right_entity], reason="one company")
    pairs = {(c.left_entity, c.right_entity) for c in engine.relationship_candidates()}
    assert (first.left_entity, first.right_entity) not in pairs


def test_answered_reviews_become_labels(engine):
    assert engine.labels() == []
    summaries = engine.reviews("boundary")
    same, different, dismissed = (summaries[i].review.review_id for i in (0, 1, 2))
    engine.resolve_review(same, "same", reason="one")
    engine.resolve_review(different, "different", reason="two")
    engine.resolve_review(dismissed, "dismiss", reason="later")
    labels = engine.labels()
    by_review = {}
    for label in labels:
        by_review.setdefault(label.review_id, []).append(label)
    assert set(by_review) == {same, different}
    assert {x.label for x in by_review[same]} == {"match"}
    assert "no_match" in {x.label for x in by_review[different]}
    scored = [x for x in labels if x.judgment_id]
    assert scored and all(x.model_ref and "fellegi_sunter_weight" in x.scores for x in scored)
    assert all(x.left_features and x.right_features for x in labels)
    assert all(x.left_id < x.right_id for x in labels)


def test_labels_read_entities_as_they_were_when_answered(engine):
    case = engine.review_case(engine.reviews("boundary")[0].review.review_id)
    entity = next(n.entity_id for n in case.nodes)
    before = set(engine.members(entity))
    engine.resolve_review(case.review.review_id, "different", reason="two")
    count = len(engine.labels())
    # Later changes to the entity do not change what the answer meant.
    engine.split_entity(entity, sorted(before)[:1], reason="later")
    assert len(engine.labels()) == count


def test_neighborhoods_are_bounded_and_keep_every_candidate(engine, monkeypatch):
    """Unbounded, real UK relationship signals chained 80% of a store into one
    neighborhood (design 07 §13.4)."""
    import thisness.relationships as relationships

    everything = engine.relationship_candidates()
    monkeypatch.setattr(relationships, "MAX_NEIGHBORHOOD", 3)
    bounded = engine.relationship_candidates()
    assert {(c.left_entity, c.right_entity) for c in bounded} == \
        {(c.left_entity, c.right_entity) for c in everything}  # fmt: skip
    assert all(c.neighborhood_size <= 3 for c in bounded)
