from __future__ import annotations

from collections import defaultdict

import pytest

from thisness.core import Actor
from thisness.edges import FellegiSunterEdgePolicy, aggregate
from thisness.engine import Thisness
from thisness.policy import ResolutionPolicy
from thisness.profiles.organization import organization
from thisness.storage import CommitConflict
from thisness.storage.duckdb.invariants import check

from .synthetic import observations, organizations


@pytest.fixture
def engine(store, ids, now) -> Thisness:
    return Thisness(store, profiles=[organization()], ids=ids, clock=lambda: now,
                    random_pairs=50_000)  # fmt: skip


def load(engine, *, hold_back: int = 0):
    """Observe the synthetic dataset. Returns (truth, observations held back), where
    held-back observations belong to organizations that have other observations."""
    truth, obs = {}, []
    for i, (org, attributes) in enumerate(observations(organizations())):
        o = engine.observe("organization", attributes, {"type": "synthetic", "id": "t",
                                                         "locator": {"i": i}})  # fmt: skip
        truth[o.observation_id] = org
        obs.append(o)
    counts = defaultdict(int)
    for org in truth.values():
        counts[org] += 1
    held, kept = [], []
    for o in obs:
        if len(held) < hold_back and counts[truth[o.observation_id]] > 1:
            held.append(o)
            counts[truth[o.observation_id]] -= 1
        else:
            kept.append(o)
    engine.ingest(kept)
    return truth, held


def b_cubed_precision(engine, truth) -> float:
    entity_of = engine.store.entities_of(list(truth))
    clusters = defaultdict(set)
    for o, e in entity_of.items():
        clusters[e].add(o)
    total = sum(
        len({x for x in clusters[entity_of[o]] if truth[x] == truth[o]})
        / len(clusters[entity_of[o]])
        for o in entity_of
    )
    return total / len(entity_of)


# --- the edge policy -----------------------------------------------------------------------


def test_edge_classes():
    policy = FellegiSunterEdgePolicy()
    row = {"cannot_link": False, "rule_verdict": None, "weight": 5.0}
    assert policy.classify(row, t_high=10) == "contested"
    assert policy.classify({**row, "weight": 12.0}, t_high=10) == "strong"
    assert policy.classify({**row, "weight": -1.0}, t_high=10) == "negative"
    assert policy.classify({**row, "rule_verdict": "MATCH", "weight": -1.0}, 10) == "strong"
    assert policy.classify({**row, "cannot_link": True, "weight": 30.0}, 10) == "cannot_link"


def test_aggregate_takes_best_support_and_any_contradiction():
    node = {"o": "o", "m1": "E", "m2": "E", "m3": "F", "m4": "G", "m5": "G"}.get
    rows = [
        ("o", "m1", "strong"), ("o", "m2", "negative"),  # E: strong but contradicted
        ("o", "m3", "strong"),  # F: clean
        ("o", "m4", "strong"), ("o", "m5", "cannot_link"),  # G: hard conflict
    ]  # fmt: skip
    folded = aggregate(rows, node)
    assert folded[("E", "o")][0] == "contested"
    assert folded[("F", "o")] == ("strong", [2])
    assert folded[("G", "o")][0] == "cannot_link"


def test_blast_radius_gate():
    policy = ResolutionPolicy()
    assert not policy.attach_needs_review(current_size=1, adding=1)  # a company's second record
    assert not policy.attach_needs_review(current_size=20, adding=5)
    assert policy.attach_needs_review(current_size=20, adding=11)
    assert policy.attach_needs_review(current_size=500, adding=26)


# --- resolution ------------------------------------------------------------------------------


def test_bulk_resolution_is_precise_and_complete(engine, store):
    truth, _ = load(engine)
    result = engine.resolve_many("organization")
    # Each common-value adjustment reports its mode: the name's, then the postcode's.
    assert result.committed and "name:name.tf_key two-pass" in result.stats["term_frequency"]
    assert set(result.outcomes) == set(truth)  # every observation leaves with an id
    assert set(store.entities_of(list(truth))) == set(truth)
    assert b_cubed_precision(engine, truth) > 0.99
    assert store.run(result.run_id).status == "committed"
    # Nothing left to resolve: a second call is a no-op.
    assert engine.resolve_many("organization").run_id is None


def test_incremental_observations_attach_to_existing_entities(engine, store):
    truth, held = load(engine, hold_back=12)
    engine.resolve_many("organization")
    result = engine.resolve_many("organization", held)
    assert "name:name.tf_key store" in result.stats["term_frequency"]
    attached = [o for o in result.outcomes.values() if o.outcome == "ATTACH_TO_EXISTING"]
    # Every attach is to the right company. How many attach depends on the threshold,
    # which on this small set is strict (see design 03 §20); 5 of 12 at the time of
    # writing.
    assert len(attached) >= 3
    for outcome in attached:
        others = [m for m in store.members(outcome.entity_id) if m != outcome.observation_id]
        assert {truth[m] for m in others} == {truth[outcome.observation_id]}


def test_resolve_one_observation(engine, store):
    truth, _ = load(engine)
    engine.resolve_many("organization")
    some = next(iter(truth))
    name = store.observations([some])[0].attributes["name"]
    outcome = engine.resolve("organization", {"name": name},
                             {"type": "crm", "id": "acct-1"})  # fmt: skip
    assert outcome.outcome in {"ATTACH_TO_EXISTING", "CREATE_ENTITY", "REQUIRES_REVIEW"}
    assert store.entity_of(outcome.observation_id) == outcome.entity_id


def test_dry_run_changes_nothing(engine, store):
    load(engine)
    engine.fit("organization")
    head = store.head_seq()
    result = engine.resolve_many("organization", commit=False)
    assert not result.committed and result.changes is not None
    assert store.run(result.run_id).status == "proposed"
    assert store._con.execute("SELECT count(*) FROM memberships").fetchall()[0][0] == 0
    # Facts were recorded (decisions, evidence); identity state did not move.
    assert store.head_seq() > head
    assert all(
        e.event_type in {"RUN_STARTED", "RUN_PROPOSED"} for e in store.ledger(since_seq=head)
    )


def test_conflicting_commit_is_rerun_once(engine, store, monkeypatch):
    load(engine)
    real = store.commit
    calls = {"n": 0}

    def flaky(changes, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise CommitConflict(changes.base_seq, ["org_x"], [])
        return real(changes, **kwargs)

    monkeypatch.setattr(store, "commit", flaky)
    result = engine.resolve_many("organization")
    assert result.committed and calls["n"] == 2
    statuses = [
        r[0] for r in store._con.execute("SELECT status FROM runs ORDER BY run_id").fetchall()
    ]
    assert statuses.count("abandoned") == 2  # the fit run and the conflicted run


def test_observations_under_review_are_held(engine, store):
    truth, _ = load(engine)
    result = engine.resolve_many("organization")
    reviews = store.open_reviews()
    assert reviews and len(result.reviews) == len(reviews)
    held = engine._held_nodes()
    for review in reviews:
        assert set(review.members) <= held | set(truth)


def test_explain_assembles_stored_records(engine, store):
    load(engine)
    result = engine.resolve_many("organization")
    multi = next(
        o for o in result.outcomes.values()
        if o.outcome == "CREATE_ENTITY" and len(store.members(o.entity_id)) > 2
    )  # fmt: skip
    explanation = engine.explain(multi.decision_id)
    assert explanation["decision"]["outcome"] == "CREATE_ENTITY"
    assert explanation["edges"]
    edge = explanation["edges"][0]
    assert edge["decision"]["decision_type"] == "pair"
    assert {e["kind"] for e in edge["evidence"]} >= {"compare.name"}


# --- operator actions ---------------------------------------------------------------------------


def test_operator_merge_split_unmerge_and_relate(engine, store):
    truth, _ = load(engine)
    engine.resolve_many("organization")
    entities = sorted({e for e in store.entities_of(list(truth)).values()
                       if store.entity(e).state == "active"})  # fmt: skip
    a, b = entities[0], entities[1]
    a_members, b_members = store.members(a), store.members(b)
    reviewer = Actor(kind="human", id="reviewer")
    engine.merge_entities(a, [b], reason="same company", actor=reviewer)
    assert engine.resolve_id(b) == [a]
    engine.split_entity(a, b_members, into=b, reason="merge was wrong", actor=reviewer)
    assert engine.resolve_id(b) == [b] and store.members(a) == a_members
    rel = engine.relate(a, "subsidiary_of", b, reason="annual report")
    assert [r.relationship_id for r in store.relationships(a)] == [rel]
    entity, snapshot = engine.get_entity(a)
    assert snapshot is not None and snapshot.version == entity.version
    assert [e.event_type for e in engine.history(entity_id=b)][-1] == "RELATIONSHIP_CREATED"
    assert check(store) == []


def _few(engine, names=("IBM Corp.", "I.B.M.", "International Business Machines")):
    engine.ingest([
        engine.observe("organization", {"name": n, "country": "US"},
                       {"type": "t", "id": "few", "locator": {"n": n}})
        for n in names
    ])  # fmt: skip


def test_small_stores_use_the_starter_model(engine):
    _few(engine)
    result = engine.resolve_many("organization")
    assert result.committed
    assert engine.model_source("organization") == "starter"
    # too few entities to count names on
    assert "name:name.tf_key off" in result.stats["term_frequency"]


def test_without_a_starter_too_few_observations_are_refused(store, ids, now):
    import dataclasses

    from thisness.storage import ThisnessError

    profile = dataclasses.replace(organization(), starter_model=None)
    engine = Thisness(store, profiles=[profile], ids=ids, clock=lambda: now)
    _few(engine)
    with pytest.raises(ThisnessError, match="too few to fit"):
        engine.resolve_many("organization")


def test_the_starter_is_replaced_once_there_is_enough_data(engine):
    _few(engine)
    engine.resolve_many("organization")
    assert engine.model_source("organization") == "starter"
    load(engine)  # a few hundred more observations
    engine.resolve_many("organization")
    assert engine.model_source("organization") == "fitted"


def test_profile_floor_raises_the_automatic_threshold(engine):
    load(engine)
    model = engine.fit("organization")
    policy = engine.edge_policy
    automatic = model.weight_for_posterior(policy.posterior)
    assert policy.threshold(model, floor=automatic + 3) == automatic + 3
    assert policy.threshold(model, floor=automatic - 3) == automatic
    assert FellegiSunterEdgePolicy(t_high=5.0).threshold(model, floor=12.0) == 5.0
