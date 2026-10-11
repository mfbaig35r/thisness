from __future__ import annotations

import dataclasses

import pytest

from thisness.engine import Thisness
from thisness.profiles.base import AttributeDefinition
from thisness.profiles.organization import organization


@pytest.fixture
def engine(store, ids, now) -> Thisness:
    return Thisness(store, profiles=[organization()], ids=ids, clock=lambda: now)


ACME = [
    {"name": "Acme Logistics LLC", "country": "US", "city": "Phoenix",
     "registration_ids": ["duns:150483782"], "email": "ap@acme-logistics.com"},
    {"name": "ACME LOGISTICS L.L.C.", "country": "United States", "city": "Phoenix",
     "registration_ids": ["duns:150483782"]},
    {"name": "Acme Logistics", "country": "USA", "city": "Phoenix",
     "registration_ids": ["duns:150483782"]},
]  # fmt: skip
ACME_DE = {"name": "Acme Logistik GmbH", "country": "DE", "city": "Berlin",
           "registration_ids": ["duns:316929113"]}  # fmt: skip


def resolve(engine, rows, source="erp"):
    obs = [engine.observe("organization", r, {"type": source, "id": f"{source}-{i}"})
           for i, r in enumerate(rows)]  # fmt: skip
    engine.resolve_many("organization", obs)
    return [engine.store.entities_of([o.observation_id])[o.observation_id] for o in obs]


def test_support_groups_values_by_the_profiles_normalizers(engine):
    entities = resolve(engine, ACME)
    assert len(set(entities)) == 1
    d = engine.dossier(entities[0])
    support = {a.attribute: a for a in d.attributes}
    # "US", "United States" and "USA" are one country; names differing only in legal
    # form and case are one name.
    assert [len(v.observations) for v in support["country"].values] == [3]
    assert [len(v.observations) for v in support["name"].values] == [3]
    assert support["name"].canonical == d.snapshot.attributes["name"]
    assert support["email"].observed == 1
    assert support["country"].values[0].sources == (("erp", 3),)
    assert d.member_count == 3 and not d.contradictions
    assert [a.attribute for a in d.attributes][:2] == ["name", "registration_ids"]


def test_contradictions_show_both_sides(engine):
    us = resolve(engine, ACME)[0]
    de = resolve(engine, [ACME_DE], source="crm")[0]
    assert us != de
    engine.merge_entities(us, [de], reason="operator error, on purpose")
    d = engine.dossier(us)
    found = {(c.attribute, c.scheme): c for c in d.contradictions}
    assert set(found) == {("registration_ids", "DUNS"), ("country", None)}
    sides = found[("country", None)].sides
    assert [(s.value, len(s.observations)) for s in sides] == [("US", 3), ("DE", 1)]
    assert sides[1].sources == (("crm", 1),)


def test_lineage_follows_merges_and_splits(engine):
    us = resolve(engine, ACME)[0]
    de = resolve(engine, [ACME_DE], source="crm")[0]
    engine.merge_entities(us, [de], reason="merge")
    d = engine.dossier(us)
    assert [e for e, _ in d.lineage.merged_from] == [de]
    # Asked for by its retired id, the dossier is the survivor's.
    assert engine.dossier(de).entity.entity_id == us
    assert engine.dossier(de).lineage.redirected_from == de
    moved = engine.members(us)[-1:]
    engine.split_entity(us, moved, reason="undo")
    d = engine.dossier(us)
    ((target, _),) = d.lineage.split_into
    assert engine.dossier(target).lineage.split_from[0][0] == us
    assert dict(d.history.events)["ENTITY_SPLIT"] == 1
    assert d.history.last_seq > d.history.created_seq


def test_relationships_and_reviews_appear(engine):
    us = resolve(engine, ACME)[0]
    de = resolve(engine, [ACME_DE], source="crm")[0]
    engine.relate(de, "subsidiary_of", us, reason="annual report")
    d = engine.dossier(us)
    (view,) = d.relationships
    assert (view.other_id, view.role, view.other_label) == (de, "object", "Acme Logistik GmbH")
    assert "Acme Logistik GmbH (" in d.to_context()


def test_context_is_deterministic_prioritized_and_never_cut_mid_record(engine):
    us = resolve(engine, ACME * 4)[0]
    de = resolve(engine, [ACME_DE], source="crm")[0]
    engine.merge_entities(us, [de], reason="merge")
    full = engine.dossier(us).to_context(100_000)
    assert full == engine.dossier(us).to_context(100_000)
    assert full.index("## Contradictions") < full.index("## Attributes") \
        < full.index("## Observations")  # fmt: skip
    for budget in (300, 600, 1200):
        short = engine.dossier(us).to_context(budget)
        assert len(short) <= budget
        assert short.endswith("omitted for length.)")
        full_lines = set(full.splitlines())
        kept = [ln for ln in short.splitlines() if ln and not ln.startswith("(")]
        assert all(ln in full_lines for ln in kept)
    # Higher priority first: a tight budget keeps contradictions, drops observations.
    tight = engine.dossier(us).to_context(600)
    assert "## Contradictions" in tight and "## Observations" not in tight


def test_context_follows_external_model_policy(store, ids, now):
    profile = organization()
    attributes = dict(profile.attributes)
    attributes["email"] = attributes["email"].model_copy(
        update={"external_model_policy": "forbidden"}
    )
    attributes["city"] = AttributeDefinition(name="city", data_type="string",
                                             external_model_policy="hash")  # fmt: skip
    engine = Thisness(store, profiles=[dataclasses.replace(profile, attributes=attributes)],
                      ids=ids, clock=lambda: now)  # fmt: skip
    us = resolve(engine, ACME)[0]
    text = engine.dossier(us).to_context()
    assert "acme-logistics.com" not in text and "withheld by policy: email" in text
    assert "Phoenix" not in text and "city: #" in text
    # The dossier object itself is complete; only the rendering for a model is filtered.
    assert any(a.attribute == "email" for a in engine.dossier(us).attributes)


def test_support_is_for_the_value_shown_not_the_most_frequent():
    from thisness.dossier import AttributeSupport, ValueSupport

    variety = ValueSupport("variety", ("obs_1", "obs_2", "obs_3"), (("wfo", 3),))
    species = ValueSupport("species", ("obs_4",), (("wfo", 1),))
    shown = AttributeSupport("rank", "species", (variety, species), observed=4).shown
    assert shown is species
    assert AttributeSupport("rank", None, (variety, species), observed=4).shown is variety
    assert AttributeSupport("rank", "form", (variety, species), observed=4).shown is None
    ident = ValueSupport("WFO:wfo-0000811441", ("obs_4",), (("wfo", 1),))
    assert AttributeSupport("identifiers", ["WFO:wfo-0000811441"], (ident,), observed=1).shown \
        is ident  # fmt: skip
