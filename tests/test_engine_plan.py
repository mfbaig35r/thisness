"""The engine's planning step, driven by hand-built consensus results, so every outcome
path is exercised regardless of what a fitted model happens to score. Each plan is
committed through the real store; the fixture then runs the invariant suite."""

from __future__ import annotations

from thisness.consensus import Boundary, Cluster, ConsensusResult
from thisness.core import ChangeSet, CreateEntity, ResolutionRun, RunStatus
from thisness.core.decisions import ConsensusOutcome as O
from thisness.engine import Thisness
from thisness.fellegi_sunter import FSModel
from thisness.policy import ResolutionPolicy
from thisness.profiles.organization import organization

from .storage_helpers import SYSTEM, World

MODEL = FSModel(entity_type="organization", comparisons=("name",), levels={"name": ("a", "b")},
                m={"name": (0.9, 0.1)}, u={"name": (0.1, 0.9)}, lam=0.2)  # fmt: skip


def setup(store, ids, now, policy=None):
    engine = Thisness(store, profiles=[organization()], ids=ids, clock=lambda: now,
                      policy=policy)  # fmt: skip
    store.register_component(MODEL.ref, MODEL.config())
    world = World(store, ids, now)
    existing = {}
    for name, state in [("E1", "active"), ("E2", "active"), ("P1", "provisional"),
                        ("E3", "active"), ("E4", "active")]:  # fmt: skip
        member = world.obs(f"existing {name}")
        entity = ids.entity("org")
        world.commit(CreateEntity(entity_id=entity, entity_type="organization", state=state,
                                  members=(member,), decision_id=world.consensus((member,))))  # fmt: skip
        existing[name] = entity
    scope = {f"s{i}": world.obs(f"new {i}") for i in range(1, 7)}
    profile = engine.profiles["organization"]
    run = ResolutionRun(run_id=ids.run(), entity_type="organization", profile_ref=profile.ref.ref,
                        base_seq=store.head_seq(), status=RunStatus.EXECUTING, plan={},
                        created_at=now)  # fmt: skip
    store.record_run(run, sorted(scope.values()))
    return engine, profile, run, existing, scope


def plan_and_commit(engine, profile, run, scope, result, existing, now):
    states = engine.store.entity_states(list(existing.values()))
    members = engine.store.members_of(list(existing.values()))
    ops, outcomes, decisions, reviews = engine._plan(
        profile, MODEL, run, set(scope.values()), result, {}, {}, members, states, now
    )
    engine.store.record_decisions(decisions)
    engine.store.commit(ChangeSet(run_id=run.run_id, base_seq=run.base_seq, ops=tuple(ops)),
                        actor=SYSTEM)  # fmt: skip
    return ops, outcomes, decisions, reviews


def test_every_outcome_path(store, ids, now):
    engine, profile, run, e, s = setup(store, ids, now)
    result = ConsensusResult(
        clusters=(
            Cluster((s["s1"], e["E1"]), O.ATTACH, e["E1"]),
            Cluster((s["s2"], e["E2"], e["P1"]), O.MERGE_PROVISIONAL, e["E2"]),
            Cluster((s["s3"], s["s4"]), O.REVIEW_CLUSTER, suggested_cut=((s["s3"],), (s["s4"],))),
            Cluster((s["s5"], e["E3"], e["E4"]), O.PROPOSE_MERGE),
            Cluster((s["s6"],), O.CREATE_ENTITY),
        ),
        boundaries=(Boundary((0, 4), ((s["s1"], s["s6"]),)),),
    )
    _, outcomes, decisions, reviews = plan_and_commit(engine, profile, run, s, result, e, now)

    assert outcomes[s["s1"]].outcome == "ATTACH_TO_EXISTING"
    assert outcomes[s["s1"]].entity_id == e["E1"]
    # A provisional entity merges into the active one without review; s2 attaches.
    assert outcomes[s["s2"]].entity_id == e["E2"]
    assert store.entity(e["P1"]).state == "retired"
    assert set(store.members(e["E2"])) >= {s["s2"]}
    # A review cluster: each new observation gets its own provisional entity (design 02 D1).
    for obs in (s["s3"], s["s4"], s["s5"]):
        assert outcomes[obs].outcome == "REQUIRES_REVIEW"
        assert store.entity(outcomes[obs].entity_id).state == "provisional"
        assert store.members(outcomes[obs].entity_id) == [obs]
    # Two active entities are never merged automatically.
    assert store.entity(e["E3"]).state == store.entity(e["E4"]).state == "active"
    assert outcomes[s["s6"]].outcome == "CREATE_ENTITY"
    kinds = sorted(r.kind for r in store.open_reviews())
    assert kinds == ["boundary", "cluster", "merge"] and len(reviews) == 3
    # The review's members, and the provisional entities of its observations, are held.
    held = engine._held_nodes()
    assert {s["s3"], s["s4"], outcomes[s["s3"]].entity_id, e["E3"], e["E4"]} <= held
    review_decision = next(d for d in decisions if d.outcome == O.REVIEW_CLUSTER)
    assert review_decision.suggested_cut is not None


def test_blast_radius_turns_an_attach_into_a_review(store, ids, now):
    policy = ResolutionPolicy(max_attach=0)
    engine, profile, run, e, s = setup(store, ids, now, policy=policy)
    result = ConsensusResult(clusters=(Cluster((s["s1"], e["E1"]), O.ATTACH, e["E1"]),))
    _, outcomes, decisions, _ = plan_and_commit(engine, profile, run, s, result, e, now)
    assert outcomes[s["s1"]].outcome == "REQUIRES_REVIEW"
    assert decisions[0].flags["policy"] == "blast_radius"
    assert store.members(e["E1"]) != [s["s1"]]


def test_review_members_and_their_provisional_entities_are_held(store, ids, now):
    """The engine downgrades strong edges touching held nodes to contested; this checks
    that a review's provisional entities are in the held set it uses."""
    engine, profile, run, e, s = setup(store, ids, now)
    result = ConsensusResult(clusters=(Cluster((s["s3"], s["s4"]), O.REVIEW_CLUSTER),))
    _, outcomes, _, _ = plan_and_commit(engine, profile, run, s, result, e, now)
    held_entity = outcomes[s["s3"]].entity_id
    assert held_entity in engine._held_nodes()
