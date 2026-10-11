from __future__ import annotations

from datetime import date

import pytest
from pydantic import TypeAdapter, ValidationError

from thisness.core import (
    Actor,
    Attach,
    ChangeSet,
    ComponentRef,
    ConsensusDecision,
    ConsensusOutcome,
    CreateEntity,
    Decision,
    Entity,
    EntityState,
    EvidenceItem,
    GroupProposal,
    IdentityMembership,
    Merge,
    Observation,
    OperatorDecision,
    PairJudgment,
    ProposalMember,
    Relate,
    RelationshipAssertion,
    ResolutionRun,
    ReviewItem,
    RunStatus,
    Score,
    SourceRef,
    Split,
    Verdict,
)

SYSTEM = Actor(kind="system", id="test")


def observation(ids, now, **overrides) -> Observation:
    fields = {
        "ids": ids,
        "entity_type": "organization",
        "attributes": {"name": "IBM Corp.", "domain": "ibm.com"},
        "source": {"type": "csv", "id": "suppliers.csv", "locator": {"row": 12}},
        "ingested_at": now,
    }
    fields.update(overrides)
    return Observation.new(**fields)


# --- Observations -------------------------------------------------------------


def test_fingerprint_ignores_key_order_and_context(ids, now):
    a = observation(ids, now, attributes={"name": "IBM", "domain": "ibm.com"})
    b = observation(
        ids, now, attributes={"domain": "ibm.com", "name": "IBM"}, context={"sentence": "..."}
    )
    assert a.observation_id != b.observation_id
    assert a.fingerprint == b.fingerprint


def test_same_text_from_two_sources_is_two_observations(ids, now):
    a = observation(ids, now, source={"type": "doc", "id": "d1", "locator": {"page": 3}})
    b = observation(ids, now, source={"type": "doc", "id": "d2", "locator": {"page": 3}})
    assert a.fingerprint != b.fingerprint


def test_attributes_must_be_json(ids, now):
    with pytest.raises(ValidationError):
        observation(ids, now, attributes={"tags": {"a", "b"}})


def test_redaction_clears_attributes_and_nothing_else(ids, now):
    obs = observation(ids, now)
    redacted = obs.model_copy(update={"attributes": None, "redacted_at": now})
    Observation.model_validate(redacted.model_dump())  # still valid
    with pytest.raises(ValidationError, match="redacted"):
        Observation.model_validate({**obs.model_dump(), "attributes": None})


def test_observation_id_must_be_an_observation_id(ids, now):
    obs = observation(ids, now)
    with pytest.raises(ValidationError, match="expected a 'obs' id"):
        Observation.model_validate({**obs.model_dump(), "observation_id": ids.decision()})


def test_records_are_immutable(ids, now):
    obs = observation(ids, now)
    with pytest.raises(ValidationError):
        obs.entity_type = "person"


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError, match="Extra inputs"):
        SourceRef(type="csv", id="x", sheet="Sheet1")


# --- Components and scores ----------------------------------------------------


def test_component_ref_depends_on_config_not_key_order():
    a = ComponentRef.of("resolver", "fellegi_sunter", "1.0", {"t_high": 11.4, "t_low": 0})
    b = ComponentRef.of("resolver", "fellegi_sunter", "1.0", {"t_low": 0, "t_high": 11.4})
    c = ComponentRef.of("resolver", "fellegi_sunter", "1.0", {"t_low": 0, "t_high": 12.0})
    assert a.ref == b.ref != c.ref
    assert a.ref.startswith("resolver:fellegi_sunter@1.0#") and len(a.ref.split("#")[1]) == 12


def test_only_calibrated_scores_may_claim_probability():
    Score(kind="fellegi_sunter_weight", value=12.4)
    Score(kind="calibrated_match_probability", value=0.99, calibration_ref="iso-v1")
    with pytest.raises(ValidationError, match="requires a calibration_ref"):
        Score(kind="calibrated_match_probability", value=0.99)
    with pytest.raises(ValidationError, match=r"in \[0, 1\]"):
        Score(kind="calibrated_match_probability", value=1.5, calibration_ref="iso-v1")


def test_evidence_links_two_different_nodes(ids, now):
    obs, ent = ids.observation(), ids.entity("org")
    item = {
        "evidence_id": ids.evidence(),
        "left_id": obs,
        "right_id": ent,
        "kind": "normalized_name_match",
        "level": "exact_key",
        "direction": "supports",
        "method_ref": "comparison:name@1#abc",
        "created_at": now,
    }
    EvidenceItem.model_validate(item)
    with pytest.raises(ValidationError, match="two different nodes"):
        EvidenceItem.model_validate({**item, "right_id": obs})
    with pytest.raises(ValidationError, match="not an observation or entity"):
        EvidenceItem.model_validate({**item, "right_id": ids.decision()})


# --- Decisions ------------------------------------------------------------------


def test_decisions_round_trip_through_one_discriminated_type(ids, now):
    adapter = TypeAdapter(Decision)
    pair = PairJudgment(
        decision_id=ids.decision(),
        actor=Actor(kind="resolver", id="fs"),
        left_id=ids.observation(),
        right_id=ids.observation(),
        verdict=Verdict.MATCH,
        scores=(Score(kind="fellegi_sunter_weight", value=14.2),),
        created_at=now,
    )
    operator = OperatorDecision(
        decision_id=ids.decision(),
        actor=Actor(kind="human", id="fb"),
        action="MERGE",
        subject_ids=(ids.entity("org"), ids.entity("org")),
        reason="duplicate masters",
        created_at=now,
    )
    for decision in (pair, operator):
        assert adapter.validate_json(adapter.dump_json(decision)) == decision


def test_attach_and_provisional_merge_need_a_target(ids, now):
    base = {
        "decision_id": ids.decision(),
        "actor": SYSTEM,
        "run_id": ids.run(),
        "members": (ids.observation(),),
        "created_at": now,
    }
    ConsensusDecision(**base, outcome=ConsensusOutcome.CREATE_ENTITY)
    with pytest.raises(ValidationError, match="requires target_entity_id"):
        ConsensusDecision(**base, outcome=ConsensusOutcome.ATTACH)


# --- Identity state -------------------------------------------------------------


def test_retirement_fields_are_consistent(ids):
    org, other = ids.entity("org"), ids.entity("org")
    Entity(entity_id=org, entity_type="organization", state="active", version=1, created_seq=1)
    Entity(
        entity_id=org,
        entity_type="organization",
        state=EntityState.RETIRED,
        version=2,
        created_seq=1,
        retired_seq=9,
        merged_into=other,
    )
    with pytest.raises(ValidationError, match="retired_seq"):
        Entity(entity_id=org, entity_type="organization", state="retired", version=2, created_seq=1)
    with pytest.raises(ValidationError, match="only a retired entity"):
        Entity(
            entity_id=org,
            entity_type="organization",
            state="active",
            version=1,
            created_seq=1,
            merged_into=other,
        )


def test_membership_validity_is_a_half_open_interval(ids):
    m = IdentityMembership(
        membership_id=ids.membership(),
        observation_id=ids.observation(),
        entity_id=ids.entity("org"),
        basis_decision_id=ids.decision(),
        valid_from_seq=5,
        valid_to_seq=9,
    )
    assert [m.is_valid_at(s) for s in (4, 5, 8, 9)] == [False, True, True, False]
    with pytest.raises(ValidationError, match="after valid_from_seq"):
        m.model_validate({**m.model_dump(), "valid_to_seq": 5})


@pytest.mark.parametrize("relation", ["same_entity", "same_as"])
def test_identity_is_never_a_relationship(ids, relation):
    with pytest.raises(ValidationError, match="identity, not a relationship"):
        RelationshipAssertion(
            relationship_id=ids.relationship(),
            relation=relation,
            subject_entity_id=ids.entity("org"),
            object_entity_id=ids.entity("org"),
            basis_decision_id=ids.decision(),
            valid_from_seq=1,
        )


def test_relationship_dates_are_ordered(ids):
    with pytest.raises(ValidationError, match="before effective_from"):
        RelationshipAssertion(
            relationship_id=ids.relationship(),
            relation="subsidiary_of",
            subject_entity_id=ids.entity("org"),
            object_entity_id=ids.entity("org"),
            basis_decision_id=ids.decision(),
            effective_from=date(2025, 1, 1),
            effective_to=date(2024, 1, 1),
            valid_from_seq=1,
        )


def test_resolved_review_cites_its_resolution(ids):
    base = {
        "review_id": ids.review(),
        "kind": "cluster",
        "decision_id": ids.decision(),
        "members": (ids.observation(),),
        "opened_seq": 3,
    }
    ReviewItem(**base, status="open")
    with pytest.raises(ValidationError, match="cites the decision"):
        ReviewItem(**base, status="resolved", closed_seq=7)


# --- Runs, proposals, change sets ---------------------------------------------


def test_run_commit_fields_are_consistent(ids, now):
    base = {
        "run_id": ids.run(),
        "entity_type": "organization",
        "profile_ref": "profile:organization@1#abc",
        "base_seq": 10,
        "plan": {},
        "created_at": now,
    }
    ResolutionRun(**base, status=RunStatus.COMMITTED, committed_seq=11)
    with pytest.raises(ValidationError, match="after its base"):
        ResolutionRun(**base, status=RunStatus.COMMITTED, committed_seq=10)
    with pytest.raises(ValidationError, match="exactly when"):
        ResolutionRun(**base, status=RunStatus.PROPOSED, committed_seq=11)


def test_a_node_appears_once_per_proposal(ids):
    node = ids.observation()
    with pytest.raises(ValidationError, match="at most once"):
        GroupProposal(
            proposal_id=ids.proposal(),
            run_id=ids.run(),
            stage_id=ids.stage(),
            batch_id="b1",
            members=(
                ProposalMember(node_id=node, shown="AWS", group_no=0),
                ProposalMember(node_id=node, shown="AWS", group_no=1),
            ),
        )


def test_split_targets_are_disjoint_and_exclude_the_source(ids):
    source, a, b = ids.entity("org"), ids.entity("org"), ids.entity("org")
    o1, o2 = ids.observation(), ids.observation()
    Split(source_id=source, targets={a: (o1,), b: (o2,)}, decision_id=ids.decision())
    with pytest.raises(ValidationError, match="two targets"):
        Split(source_id=source, targets={a: (o1,), b: (o1,)}, decision_id=ids.decision())
    with pytest.raises(ValidationError, match="source cannot be a split target"):
        Split(source_id=source, targets={source: (o1,)}, decision_id=ids.decision())


def test_merge_survivor_is_not_merged(ids):
    a = ids.entity("org")
    with pytest.raises(ValidationError, match="survivor cannot also be merged"):
        Merge(survivor_id=a, merged_ids=(a,), decision_id=ids.decision())


def test_relate_op_rejects_identity(ids):
    with pytest.raises(ValidationError, match="identity, not a relationship"):
        Relate(
            relationship_id=ids.relationship(),
            relation="same_entity",
            subject_entity_id=ids.entity("org"),
            object_entity_id=ids.entity("org"),
            decision_id=ids.decision(),
        )


def test_change_set_round_trips_and_lists_its_decisions(ids):
    d1, d2 = ids.decision(), ids.decision()
    org, obs = ids.entity("org"), ids.observation()
    changes = ChangeSet(
        run_id=ids.run(),
        base_seq=0,
        ops=(
            CreateEntity(
                entity_id=org,
                entity_type="organization",
                state="provisional",
                members=(obs,),
                decision_id=d1,
            ),
            Attach(observation_id=ids.observation(), entity_id=org, decision_id=d2),
        ),
    )
    assert changes.decision_ids == {d1, d2}
    assert ChangeSet.model_validate_json(changes.model_dump_json()) == changes
    with pytest.raises(ValidationError):
        ChangeSet(base_seq=0, ops=())
