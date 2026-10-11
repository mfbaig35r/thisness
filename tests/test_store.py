from __future__ import annotations

import random
from collections import Counter
from datetime import date

import pyarrow as pa
import pytest

from thisness.core import (
    Actor,
    Attach,
    CloseReview,
    ConsensusDecision,
    CreateEntity,
    EventType,
    EvidenceItem,
    GroupProposal,
    Merge,
    Observation,
    OpenReview,
    PairJudgment,
    Promote,
    ProposalMember,
    ReadSet,
    Relate,
    ResolutionRun,
    ReviewDecision,
    RevokeRelationship,
    RunStatus,
    Score,
    Split,
    StageExecution,
    SuggestedCut,
    Verdict,
)
from thisness.storage import (
    CommitConflict,
    InvalidOperation,
    NotFound,
    StaleVersion,
    UnknownDecision,
)
from thisness.storage.duckdb import DuckDBStore
from thisness.storage.duckdb.invariants import check

from .storage_helpers import World

# --- lifecycle and ingest ---------------------------------------------------------


def test_file_store_migrates_once_and_persists(tmp_path, ids, now):
    path = str(tmp_path / "identity.duckdb")
    store = DuckDBStore(path, ids=ids, clock=lambda: now)
    world = World(store, ids, now)
    obs = world.obs("IBM")
    store.close()
    reopened = DuckDBStore(path, ids=ids, clock=lambda: now)
    assert reopened.schema_version() == 3
    assert [o.observation_id for o in reopened.observations([obs])] == [obs]
    assert check(reopened) == []
    reopened.close()


def test_a_version_one_store_upgrades_in_place(tmp_path, ids, now):
    import duckdb

    path = str(tmp_path / "old.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)")
    from importlib import resources

    first = resources.files("thisness.storage.duckdb") / "migrations" / "0001_initial.sql"
    con.execute(
        first.read_text().replace("CREATE TABLE meta (", "CREATE TABLE IF NOT EXISTS meta (")
    )
    con.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    con.close()
    store = DuckDBStore(path, ids=ids, clock=lambda: now)
    assert store.schema_version() == 3
    assert store.constraints() == []
    store.close()


def test_ingest_is_idempotent_by_fingerprint(world, store, ids, now):
    def make():
        return Observation.new(
            ids=ids,
            entity_type="organization",
            attributes={"name": "IBM"},
            source={"type": "csv", "id": "f.csv", "locator": {"row": 1}},
            ingested_at=now,
        )

    first, second, third = make(), make(), make()
    result = store.ingest([first, second])  # duplicate within one batch
    assert result.inserted == (first.observation_id,)
    assert result.existing == {second.observation_id: first.observation_id}
    again = store.ingest([third])  # duplicate of an earlier batch
    assert again.inserted == () and again.existing == {third.observation_id: first.observation_id}
    events = [e.event_type for e in store.ledger()]
    assert events == [EventType.OBSERVATION_INGESTED]


# --- create, attach, versions, snapshots ---------------------------------------------


def test_create_and_attach_version_once_per_commit(world, store, ids):
    a, b, c = world.obs("IBM"), world.obs("IBM"), world.obs("IBM Corp.")
    org = ids.entity("org")
    world.commit(
        CreateEntity(
            entity_id=org,
            entity_type="organization",
            state="active",
            members=(a, b),
            decision_id=world.consensus((a, b)),
        )
    )
    assert store.entity(org).version == 1  # three events, one commit
    world.commit(Attach(observation_id=c, entity_id=org, decision_id=world.consensus((c,))))
    assert store.entity(org).version == 2
    assert store.members(org) == sorted([a, b, c])
    assert store.snapshot(org).version == 2
    assert store.snapshot(org).attributes == {"name": "IBM"}  # most frequent value
    assert store.snapshot(org, version=1).derived_from == tuple(sorted([a, b]))


def test_point_in_time_membership(world, store, ids):
    a, b = world.obs("IBM"), world.obs("IBM Corp.")
    org1, org2 = ids.entity("org"), ids.entity("org")
    first = world.commit(
        CreateEntity(
            entity_id=org1,
            entity_type="organization",
            state="active",
            members=(a,),
            decision_id=world.consensus((a,)),
        ),
        CreateEntity(
            entity_id=org2,
            entity_type="organization",
            state="active",
            members=(b,),
            decision_id=world.consensus((b,)),
        ),
    )
    merged = world.commit(Merge(survivor_id=org1, merged_ids=(org2,), decision_id=world.decision()))
    assert store.entity_of(b, as_of_seq=first.last_seq) == org2
    assert store.entity_of(b, as_of_seq=merged.last_seq) == org1
    assert store.entity_of(b) == org1
    assert store.members(org2, as_of_seq=first.last_seq) == [b]
    assert store.members(org2) == []


# --- rejected commits write nothing ----------------------------------------------------


def _setup_two_entities(world, ids):
    a, b, c = world.obs("IBM"), world.obs("Acme"), world.obs("Loose")
    org, acme = ids.entity("org"), ids.entity("org")
    world.commit(
        CreateEntity(
            entity_id=org,
            entity_type="organization",
            state="active",
            members=(a,),
            decision_id=world.consensus((a,)),
        ),
        CreateEntity(
            entity_id=acme,
            entity_type="organization",
            state="provisional",
            members=(b,),
            decision_id=world.consensus((b,)),
        ),
    )
    return a, b, c, org, acme


@pytest.mark.parametrize(
    "case",
    [
        "unknown_decision",
        "attach_member",
        "create_existing",
        "type_mismatch",
        "promote_active",
        "split_target_live",
        "split_empties_source",
        "split_non_member",
        "missing_observation",
        "base_ahead",
    ],
)
def test_rejected_commits_write_nothing(case, world, store, ids):
    a, b, c, org, acme = _setup_two_entities(world, ids)
    person_obs = world.obs("M. Khan", entity_type="person")
    d = world.decision()
    before = world.row_counts()
    head = store.head_seq()
    ops, error, kwargs = {
        "unknown_decision": (
            (Attach(observation_id=c, entity_id=org, decision_id=ids.decision()),),
            UnknownDecision,
            {},
        ),
        "attach_member": (
            (Attach(observation_id=a, entity_id=acme, decision_id=d),),
            InvalidOperation,
            {},
        ),
        "create_existing": (
            (
                CreateEntity(
                    entity_id=org, entity_type="organization", state="active", decision_id=d
                ),
            ),
            InvalidOperation,
            {},
        ),
        "type_mismatch": (
            (Attach(observation_id=person_obs, entity_id=org, decision_id=d),),
            InvalidOperation,
            {},
        ),
        "promote_active": ((Promote(entity_id=org, decision_id=d),), InvalidOperation, {}),
        "split_target_live": (
            (Split(source_id=org, targets={acme: (a,)}, decision_id=d),),
            InvalidOperation,
            {},
        ),
        "split_empties_source": (
            (Split(source_id=org, targets={ids.entity("org"): (a,)}, decision_id=d),),
            InvalidOperation,
            {},
        ),
        "split_non_member": (
            (Split(source_id=org, targets={ids.entity("org"): (b,)}, decision_id=d),),
            InvalidOperation,
            {},
        ),
        "missing_observation": (
            (Attach(observation_id=ids.observation(), entity_id=org, decision_id=d),),
            InvalidOperation,
            {},
        ),
        "base_ahead": (
            (Attach(observation_id=c, entity_id=org, decision_id=d),),
            InvalidOperation,
            {"base_seq": head + 5},
        ),
    }[case]
    with pytest.raises(error):
        world.commit(*ops, **kwargs)
    assert world.row_counts() == before
    assert store.head_seq() == head


def test_valid_ops_before_a_rejected_op_are_discarded(world, store, ids):
    _, _, c, org, _ = _setup_two_entities(world, ids)
    d = world.decision()
    before = world.row_counts()
    with pytest.raises(InvalidOperation, match="op 1"):
        world.commit(
            Attach(observation_id=c, entity_id=org, decision_id=d),  # valid on its own
            Promote(entity_id=org, decision_id=d),  # org is active: rejected
        )
    assert world.row_counts() == before
    assert store.entity_of(c) is None


# --- merge, split, unmerge, resolution ---------------------------------------------------


def test_merge_moves_memberships_and_retires(world, store, ids):
    a, b, c = world.obs("IBM"), world.obs("IBM Corp."), world.obs("I.B.M.")
    org1, org2 = ids.entity("org"), ids.entity("org")
    world.commit(
        CreateEntity(
            entity_id=org1,
            entity_type="organization",
            state="active",
            members=(a,),
            decision_id=world.consensus((a,)),
        ),
        CreateEntity(
            entity_id=org2,
            entity_type="organization",
            state="active",
            members=(b, c),
            decision_id=world.consensus((b, c)),
        ),
    )
    world.commit(
        Merge(
            survivor_id=org1,
            merged_ids=(org2,),
            decision_id=world.decision(),
            expected_versions={org1: 1, org2: 1},
        )
    )
    loser = store.entity(org2)
    assert loser.state == "retired" and loser.merged_into == org1
    assert store.members(org1) == sorted([a, b, c])
    assert store.resolve_id(org2) == [org1]
    merged_event = next(e for e in store.ledger(entity_id=org2) if e.event_type == "ENTITY_MERGED")
    assert merged_event.payload["members_at_retirement"] == sorted([b, c])


def test_stale_versions_are_rejected(world, store, ids):
    a, b = world.obs("IBM"), world.obs("IBM Corp.")
    org1, org2 = ids.entity("org"), ids.entity("org")
    world.commit(
        CreateEntity(
            entity_id=org1,
            entity_type="organization",
            state="active",
            members=(a,),
            decision_id=world.consensus((a,)),
        ),
        CreateEntity(
            entity_id=org2,
            entity_type="organization",
            state="active",
            members=(b,),
            decision_id=world.consensus((b,)),
        ),
    )
    with pytest.raises(StaleVersion):
        world.commit(
            Merge(
                survivor_id=org1,
                merged_ids=(org2,),
                decision_id=world.decision(),
                expected_versions={org1: 7},
            )
        )
    with pytest.raises(StaleVersion):
        world.commit(
            Split(
                source_id=org1,
                targets={ids.entity("org"): (a,)},
                decision_id=world.decision(),
                expected_version=3,
            )
        )


def test_unmerge_reactivates_and_partial_split_is_ambiguous(world, store, ids):
    a, b, c = world.obs("IBM"), world.obs("Intl Business Machines"), world.obs("IBM Research")
    org_a, org_b = ids.entity("org"), ids.entity("org")
    world.commit(
        CreateEntity(
            entity_id=org_a,
            entity_type="organization",
            state="active",
            members=(a,),
            decision_id=world.consensus((a,)),
        ),
        CreateEntity(
            entity_id=org_b,
            entity_type="organization",
            state="active",
            members=(b, c),
            decision_id=world.consensus((b, c)),
        ),
    )
    world.commit(Merge(survivor_id=org_a, merged_ids=(org_b,), decision_id=world.decision()))

    # Partial split: only c leaves, into a new entity. org_b now resolves two ways.
    org_d = ids.entity("org")
    world.commit(Split(source_id=org_a, targets={org_d: (c,)}, decision_id=world.decision()))
    assert store.resolve_id(org_b) == sorted([org_a, org_d])

    # Unmerge: b goes back to org_b, which is reactivated. Its id means what it meant.
    world.commit(Split(source_id=org_a, targets={org_b: (b,)}, decision_id=world.decision()))
    assert store.entity(org_b).state == "active"
    assert store.resolve_id(org_b) == [org_b]
    assert store.members(org_b) == [b]


def test_create_then_merge_in_one_commit(world, store, ids):
    a, b = world.obs("IBM"), world.obs("IBM Corp.")
    org1, org2 = ids.entity("org"), ids.entity("org")
    d = world.decision()
    result = world.commit(
        CreateEntity(
            entity_id=org1,
            entity_type="organization",
            state="provisional",
            members=(a,),
            decision_id=d,
        ),
        CreateEntity(
            entity_id=org2,
            entity_type="organization",
            state="provisional",
            members=(b,),
            decision_id=d,
        ),
        Merge(survivor_id=org1, merged_ids=(org2,), decision_id=d),
    )
    assert store.members(org1) == sorted([a, b])
    assert store.entity(org2).state == "retired"
    assert set(result.touched_entity_ids) == {org1, org2}


def test_promote(world, store, ids):
    a = world.obs("Acme")
    org = ids.entity("org")
    world.commit(
        CreateEntity(
            entity_id=org,
            entity_type="organization",
            state="provisional",
            members=(a,),
            decision_id=world.consensus((a,)),
        )
    )
    world.commit(Promote(entity_id=org, decision_id=world.decision("PROMOTE")))
    assert store.entity(org).state == "active"
    assert store.entity(org).version == 2


def test_missing_entity_is_not_found(store, ids):
    with pytest.raises(NotFound):
        store.entity(ids.entity("org"))


# --- relationships and reviews --------------------------------------------------------------


def test_relationships_never_touch_identity(world, store, ids):
    a, b = world.obs("IBM"), world.obs("IBM Research")
    parent, child = ids.entity("org"), ids.entity("org")
    world.commit(
        CreateEntity(
            entity_id=parent,
            entity_type="organization",
            state="active",
            members=(a,),
            decision_id=world.consensus((a,)),
        ),
        CreateEntity(
            entity_id=child,
            entity_type="organization",
            state="active",
            members=(b,),
            decision_id=world.consensus((b,)),
        ),
    )
    versions = (store.entity(parent).version, store.entity(child).version)
    rel = ids.relationship()
    world.commit(
        Relate(
            relationship_id=rel,
            relation="subsidiary_of",
            subject_entity_id=child,
            object_entity_id=parent,
            effective_from=date(2020, 1, 1),
            decision_id=world.decision("RELATE"),
        )
    )
    assert [r.relation for r in store.relationships(parent)] == ["subsidiary_of"]
    assert (store.entity(parent).version, store.entity(child).version) == versions
    world.commit(RevokeRelationship(relationship_id=rel, decision_id=world.decision("RELATE")))
    assert store.relationships(parent) == []
    with pytest.raises(InvalidOperation, match="not open"):
        world.commit(RevokeRelationship(relationship_id=rel, decision_id=world.decision()))


def test_reviews_open_and_close(world, store, ids, now):
    a = world.obs("Apple")
    review = ids.review()
    world.commit(
        OpenReview(
            review_id=review, kind="cluster", members=(a,), decision_id=world.consensus((a,))
        )
    )
    assert [r.review_id for r in store.open_reviews()] == [review]
    verdict = ReviewDecision(
        decision_id=ids.decision(),
        actor=Actor(kind="human", id="r"),
        review_id=review,
        verdict=Verdict.NO_MATCH,
        reason="different",
        created_at=now,
    )
    store.record_decisions([verdict])
    world.commit(CloseReview(review_id=review, status="resolved", decision_id=verdict.decision_id))
    assert store.open_reviews() == []
    with pytest.raises(InvalidOperation, match="not open"):
        world.commit(
            CloseReview(review_id=review, status="dismissed", decision_id=verdict.decision_id)
        )


# --- conflicts against the run's base ---------------------------------------------------------


def test_conflict_on_entities_read_after_base(world, store, ids):
    a, b, c = world.obs("IBM"), world.obs("IBM Corp."), world.obs("Acme")
    org, other = ids.entity("org"), ids.entity("org")
    world.commit(
        CreateEntity(
            entity_id=org,
            entity_type="organization",
            state="active",
            members=(a,),
            decision_id=world.consensus((a,)),
        ),
        CreateEntity(
            entity_id=other,
            entity_type="organization",
            state="active",
            members=(c,),
            decision_id=world.consensus((c,)),
        ),
    )
    base = store.head_seq()
    world.commit(Attach(observation_id=b, entity_id=org, decision_id=world.consensus((b,))))
    late = world.obs("I.B.M.")
    with pytest.raises(CommitConflict) as raised:
        world.commit(
            Attach(observation_id=late, entity_id=org, decision_id=world.decision()),
            base_seq=base,
            read_set=ReadSet(entity_ids=frozenset({org})),
        )
    assert raised.value.entity_ids == [org]
    # Reading an entity nobody touched since the base is not a conflict.
    world.commit(
        Attach(observation_id=late, entity_id=other, decision_id=world.decision()),
        base_seq=base,
        read_set=ReadSet(entity_ids=frozenset({other})),
    )


def test_conflict_on_blocking_keys_closes_the_create_race(world, store, ids):
    a, b = world.obs("Acme Logistics"), world.obs("ACME LOGISTICS")
    store.write_features(
        pa.table(
            {
                "observation_id": [a, b],
                "feature": ["name.key"] * 2,
                "ordinal": [0, 0],
                "value": ["acmelogistics"] * 2,
                "value_num": [None, None],
                "normalizer_ref": ["normalizer:org_name_key@1#abc"] * 2,
            }
        )
    )
    base = store.head_seq()
    # Run 1 creates an entity for a; run 2, which read the same block at the same base,
    # must not also create one for b.
    world.commit(
        CreateEntity(
            entity_id=ids.entity("org"),
            entity_type="organization",
            state="active",
            members=(a,),
            decision_id=world.consensus((a,)),
        ),
        base_seq=base,
        read_set=ReadSet(blocking_keys=frozenset({"name.key=acmelogistics"})),
    )
    with pytest.raises(CommitConflict) as raised:
        world.commit(
            CreateEntity(
                entity_id=ids.entity("org"),
                entity_type="organization",
                state="active",
                members=(b,),
                decision_id=world.consensus((b,)),
            ),
            base_seq=base,
            read_set=ReadSet(blocking_keys=frozenset({"name.key=acmelogistics"})),
        )
    assert raised.value.blocking_keys == ["name.key=acmelogistics"]


# --- runs and facts ---------------------------------------------------------------------------


def _record_run(store, ids, now, scope):
    profile = "profile:organization@1#000000000000"
    run = ResolutionRun(
        run_id=ids.run(),
        entity_type="organization",
        profile_ref=profile,
        base_seq=store.head_seq(),
        status=RunStatus.EXECUTING,
        plan={},
        created_at=now,
    )
    store.record_run(run, scope)
    from thisness.core import ComponentRef

    ref = ComponentRef(kind="profile", name="organization", version="1", config_hash="0" * 64)
    store.register_component(ref, {})
    return run


def test_run_commit_marks_the_run(world, store, ids, now):
    a = world.obs("IBM")
    run = _record_run(store, ids, now, [a])
    org = ids.entity("org")
    result = world.commit(
        CreateEntity(
            entity_id=org,
            entity_type="organization",
            state="active",
            members=(a,),
            decision_id=world.consensus((a,), run_id=run.run_id),
        ),
        run_id=run.run_id,
    )
    stored = store.run(run.run_id)
    assert stored.status == RunStatus.COMMITTED and stored.committed_seq == result.last_seq
    with pytest.raises(InvalidOperation, match="already committed"):
        world.commit(Promote(entity_id=org, decision_id=world.decision()), run_id=run.run_id)


def test_facts_round_trip(world, store, ids, now):
    a, b = world.obs("IBM"), world.obs("IBM Corp.")
    run = _record_run(store, ids, now, [a, b])
    stage = StageExecution(
        stage_id=ids.stage(),
        run_id=run.run_id,
        kind="verify",
        producer_ref="profile:organization@1#000000000000",
        family="llm:v1",
        view="raw",
        started_at=now,
    )
    store.record_stage(stage)
    evidence = EvidenceItem(
        evidence_id=ids.evidence(),
        left_id=a,
        right_id=b,
        kind="normalized_name_match",
        level="token_typo",
        direction="supports",
        value=0.93,
        score=Score(kind="jaro_winkler", value=0.93),
        method_ref="profile:organization@1#000000000000",
        inputs={"left": "ibm", "right": "ibm corp"},
        run_id=run.run_id,
        created_at=now,
    )
    store.record_evidence([evidence])
    assert store.evidence([evidence.evidence_id]) == [evidence]

    proposal = GroupProposal(
        proposal_id=ids.proposal(),
        run_id=run.run_id,
        stage_id=stage.stage_id,
        batch_id="b1",
        members=(
            ProposalMember(node_id=a, shown="IBM", group_no=0),
            ProposalMember(node_id=b, shown="IBM Corp.", group_no=0, uncertain=True),
        ),
    )
    store.record_proposals([proposal])
    assert store.proposals(run.run_id) == [proposal]

    judgment = PairJudgment(
        decision_id=ids.decision(),
        actor=Actor(kind="resolver", id="fs"),
        run_id=run.run_id,
        left_id=a,
        right_id=b,
        verdict=Verdict.MATCH,
        scores=(Score(kind="fellegi_sunter_weight", value=14.2),),
        evidence_ids=(evidence.evidence_id,),
        created_at=now,
    )
    consensus = ConsensusDecision(
        decision_id=ids.decision(),
        actor=Actor(kind="system", id="c"),
        run_id=run.run_id,
        members=(a, b),
        outcome="REVIEW_CLUSTER",
        flags={"conflicts": 1},
        suggested_cut=SuggestedCut(keep=(a,), move=(b,), disagreements=1),
        created_at=now,
    )
    store.record_decisions([judgment, consensus])
    assert store.decisions([judgment.decision_id, consensus.decision_id]) == sorted(
        [judgment, consensus], key=lambda d: d.decision_id
    )


def test_blocking_and_frequencies(world, store, ids, now):
    names = ["ibm", "ibm", "ibm", "acme", "acme", "zeta"]
    obs = [world.obs(n) for n in names]
    run = _record_run(store, ids, now, obs)
    store.write_features(
        pa.table(
            {
                "observation_id": obs,
                "feature": ["name.key"] * 6,
                "ordinal": [0] * 6,
                "value": names,
                "value_num": [None] * 6,
                "normalizer_ref": ["normalizer:org_name_key@1#abc"] * 6,
            }
        )
    )
    pairs = store.blocking_pairs(run.run_id, "name.key", max_block=2).to_pylist()
    assert pairs == [{"left_id": obs[3], "right_id": obs[4], "value": "acme"}]
    assert store.oversize_blocks(run.run_id, "name.key", 2) == [("ibm", 3)]

    org = ids.entity("org")
    world.commit(
        CreateEntity(
            entity_id=org,
            entity_type="organization",
            state="active",
            members=tuple(obs[:3]),
            decision_id=world.consensus(tuple(obs[:3])),
        )
    )
    by_entity = dict(zip(*store.feature_frequencies("name.key").to_pydict().values(), strict=True))
    by_obs = dict(
        zip(
            *store.feature_frequencies("name.key", by="observation").to_pydict().values(),
            strict=True,
        )
    )
    assert by_obs["ibm"] == 3 and by_entity["ibm"] == 1  # one company, three records
    assert by_entity["acme"] == 2  # unresolved observations count as their own entity
    store.drop_features("normalizer:org_name_key@1#abc")
    assert store.blocking_pairs(run.run_id, "name.key", 10).num_rows == 0


# --- the invariant suite itself -----------------------------------------------------------------


def test_invariants_catch_writes_that_bypass_commit(world, store, ids):
    a, b = world.obs("IBM"), world.obs("Acme")
    org = ids.entity("org")
    world.commit(
        CreateEntity(
            entity_id=org,
            entity_type="organization",
            state="active",
            members=(a,),
            decision_id=world.consensus((a,)),
        )
    )
    con = store._con
    con.execute(
        "INSERT INTO memberships VALUES (?, ?, ?, ?, 999, NULL)",
        [ids.membership(), b, org, ids.decision()],
    )
    con.execute("UPDATE entities SET version = 42 WHERE entity_id = ?", [org])
    found = {v.check for v in check(store)}
    assert "replay memberships: rows in the table but not the ledger" in found
    assert "replay entities: rows in the table but not the ledger" in found
    assert "every membership, relationship and review cites a recorded decision" in found
    # Leave the store consistent for the fixture's own check.
    con.execute("DELETE FROM memberships WHERE observation_id = ?", [b])
    con.execute("UPDATE entities SET version = 1 WHERE entity_id = ?", [org])


def test_random_operation_sequences_keep_every_invariant(world, store, ids):
    """200 random merges, splits, unmerges, attaches and promotions; the invariant
    suite (including ledger replay) must hold after every commit."""
    rng = random.Random(11)
    observations = [world.obs(f"org {i}") for i in range(40)]
    unattached = list(observations)
    live: list[str] = []
    retired: list[str] = []
    done: Counter[str] = Counter()
    for step in range(200):
        d = world.decision()
        choice = rng.random()
        try:
            if unattached and (choice < 0.3 or not live):
                members = tuple(
                    unattached.pop() for _ in range(min(len(unattached), rng.randint(1, 3)))
                )
                entity = ids.entity("org")
                world.commit(
                    CreateEntity(
                        entity_id=entity,
                        entity_type="organization",
                        state=rng.choice(["active", "provisional"]),
                        members=members,
                        decision_id=d,
                    )
                )
                live.append(entity)
                done["create"] += 1
            elif unattached and choice < 0.4:
                world.commit(
                    Attach(
                        observation_id=unattached.pop(), entity_id=rng.choice(live), decision_id=d
                    )
                )
                done["attach"] += 1
            elif len(live) >= 2 and choice < 0.65:
                survivor, loser = rng.sample(live, 2)
                world.commit(Merge(survivor_id=survivor, merged_ids=(loser,), decision_id=d))
                live.remove(loser)
                retired.append(loser)
                done["merge"] += 1
            elif choice < 0.9:
                source = rng.choice(live)
                members = store.members(source)
                if len(members) < 2:
                    continue
                moved = tuple(rng.sample(members, rng.randint(1, len(members) - 1)))
                target = rng.choice(retired) if retired and rng.random() < 0.5 else None
                target = target or ids.entity("org")
                world.commit(Split(source_id=source, targets={target: moved}, decision_id=d))
                if target in retired:
                    retired.remove(target)
                    done["unmerge"] += 1
                else:
                    done["split"] += 1
                live.append(target)
            else:
                entity = rng.choice(live)
                if store.entity(entity).state == "provisional":
                    world.commit(Promote(entity_id=entity, decision_id=d))
                    done["promote"] += 1
        except InvalidOperation:
            pass
        violations = check(store)
        assert violations == [], f"step {step}: {violations}"
    # Every operation kind was actually exercised, not just attempted.
    assert all(
        done[k] >= 3 for k in ("create", "attach", "merge", "split", "unmerge", "promote")
    ), done
