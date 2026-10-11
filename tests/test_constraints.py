from __future__ import annotations

import pytest

from thisness.core import AddConstraint, Attach, CreateEntity, Merge, RevokeConstraint, Split
from thisness.storage import InvalidOperation

from .storage_helpers import World


def two_entities(world: World, ids):
    a, b, c = world.obs("Acme Logistics"), world.obs("Acme Logistcs"), world.obs("Acme Holdings")
    x, y = ids.entity("org"), ids.entity("org")
    world.commit(
        CreateEntity(entity_id=x, entity_type="organization", state="active", members=(a,),
                     decision_id=world.consensus((a,))),
        CreateEntity(entity_id=y, entity_type="organization", state="active", members=(c,),
                     decision_id=world.consensus((c,))),
    )  # fmt: skip
    return a, b, c, x, y


def cannot(world, ids, left, right):
    cid = ids.constraint()
    lo, hi = sorted((left, right))
    world.commit(AddConstraint(constraint_id=cid, kind="cannot_link", left_id=lo, right_id=hi,
                               decision_id=world.decision()))  # fmt: skip
    return cid


def test_constraints_are_recorded_and_revocable(world, store, ids):
    a, _, c, _, _ = two_entities(world, ids)
    cid = cannot(world, ids, a, c)
    assert [k.constraint_id for k in store.constraints([a])] == [cid]
    world.commit(RevokeConstraint(constraint_id=cid, decision_id=world.decision()))
    assert store.constraints([a]) == []
    assert len(store.constraints([a], open_only=False)) == 1
    with pytest.raises(InvalidOperation, match="not open"):
        world.commit(RevokeConstraint(constraint_id=cid, decision_id=world.decision()))


def test_a_constraint_must_name_two_existing_observations(world, ids):
    a, *_ = two_entities(world, ids)
    ghost = ids.observation()
    lo, hi = sorted((a, ghost))
    with pytest.raises(InvalidOperation, match="does not exist"):
        world.commit(AddConstraint(constraint_id=ids.constraint(), kind="cannot_link",
                                   left_id=lo, right_id=hi, decision_id=world.decision()))  # fmt: skip


def test_cannot_link_blocks_every_way_of_joining(world, store, ids):
    a, b, c, x, y = two_entities(world, ids)
    cannot(world, ids, a, c)
    with pytest.raises(InvalidOperation, match="revoke it first"):
        world.commit(Merge(survivor_id=x, merged_ids=(y,), decision_id=world.decision()))
    cannot(world, ids, b, c)
    with pytest.raises(InvalidOperation, match="revoke it first"):
        world.commit(Attach(observation_id=b, entity_id=y, decision_id=world.decision()))
    # A commit that would join them is rejected whole: nothing it did survives.
    assert store.members(x) == [a] and store.entity_of(b) is None


def test_a_cannot_link_inside_one_entity_is_refused(world, ids):
    a, b, _, x, _ = two_entities(world, ids)
    world.commit(Attach(observation_id=b, entity_id=x, decision_id=world.decision()))
    with pytest.raises(InvalidOperation, match="revoke it first"):
        cannot(world, ids, a, b)


def test_revoking_allows_the_merge(world, store, ids):
    a, _, c, x, y = two_entities(world, ids)
    cid = cannot(world, ids, a, c)
    world.commit(RevokeConstraint(constraint_id=cid, decision_id=world.decision()))
    world.commit(Merge(survivor_id=x, merged_ids=(y,), decision_id=world.decision()))
    assert store.members(x) == sorted([a, c])


def test_must_link_is_advisory(world, store, ids):
    a, b, _, x, _ = two_entities(world, ids)
    world.commit(Attach(observation_id=b, entity_id=x, decision_id=world.decision()))
    lo, hi = sorted((a, b))
    world.commit(AddConstraint(constraint_id=ids.constraint(), kind="must_link", left_id=lo,
                               right_id=hi, decision_id=world.decision()))  # fmt: skip
    world.commit(
        Split(source_id=x, targets={ids.entity("org"): (b,)}, decision_id=world.decision())
    )
    assert store.entity_of(a) != store.entity_of(b)


def test_constraints_survive_merges_because_they_anchor_on_observations(world, ids):
    _, b, c, x, y = two_entities(world, ids)
    z = ids.entity("org")
    world.commit(CreateEntity(entity_id=z, entity_type="organization", state="active",
                              members=(b,), decision_id=world.consensus((b,))))  # fmt: skip
    cannot(world, ids, b, c)
    world.commit(Merge(survivor_id=x, merged_ids=(z,), decision_id=world.decision()))
    # b now lives in x; y still may not join it.
    with pytest.raises(InvalidOperation, match="revoke it first"):
        world.commit(Merge(survivor_id=y, merged_ids=(x,), decision_id=world.decision()))
