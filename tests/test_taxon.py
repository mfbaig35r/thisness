from __future__ import annotations

from types import SimpleNamespace

import pytest

from thisness.canonicalize import AcceptedByAuthority, ByProfile, MostFrequentValue
from thisness.comparisons.taxon import (
    AcceptedByReference,
    AssertionComparison,
    AuthorshipComparison,
    KingdomConflict,
    TaxonNameComparison,
)
from thisness.engine import Thisness
from thisness.fellegi_sunter import FSModel
from thisness.normalize.taxon import author_key, epithet_stem, parse
from thisness.profiles.taxon import taxon
from thisness.storage.duckdb.invariants import check
from thisness.training import register_model


@pytest.mark.parametrize(
    ("raw", "canonical", "authors", "basionym", "rank", "extra"),
    [
        ("Caesalpinia pulcherrima (L.) Sw.", "caesalpinia pulcherrima", "sw", "l", None, {}),
        ("Poinciana pulcherrima L.", "poinciana pulcherrima", "l", None, None, {}),
        ("CAESALPINIA PULCHERRIMA (L.) SW.", "caesalpinia pulcherrima", "sw", "l", None, {}),
        ("Rosa canina var. dumalis Baker", "rosa canina dumalis", "baker", None, "var.", {}),
        ("Quercus robur ssp. pedunculiflora (K.Koch) Menitsky",
         "quercus robur pedunculiflora", "menitsky", "kkoch", "subsp.", {}),
        ("Picea abies (L.) H. Karst.", "picea abies", "hkarst", "l", None, {}),
        ("Rhododendron ponticum L. ex Spreng.", "rhododendron ponticum", "spreng", None, None, {}),
        ("Mentha \u00d7 piperita L.", "mentha piperita", "l", None, None, {"hybrid": True}),
        ("x Cupressocyparis leylandii", "cupressocyparis leylandii", None, None, None,
         {"hybrid": True}),
        ("Abies alba auct. non Mill.", "abies alba", None, None, None,
         {"flags": frozenset({"misapplied"})}),
        ("Acer platanoides 'Crimson King'", "acer platanoides", None, None, None, {}),
        ("Aster novae-angliae L.", "aster novae-angliae", "l", None, None, {}),
        ("Ranunculus aquatilis Linn.", "ranunculus aquatilis", "l", None, None, {}),
    ],
)  # fmt: skip
def test_names_parse(raw, canonical, authors, basionym, rank, extra):
    parsed = parse(raw)
    assert (parsed.canonical, parsed.authors, parsed.basionym_authors, parsed.rank) == \
        (canonical, authors, basionym, rank)  # fmt: skip
    for key, value in extra.items():
        assert getattr(parsed, key) == value


def test_what_is_not_a_species_name():
    assert parse("Quercus sp.").epithet is None
    assert parse("C. pulcherrima").abbreviated_genus
    assert parse("12345") is None and parse("Hook.f.") is None


def test_epithet_stems_and_author_keys():
    assert {epithet_stem(e) for e in ("pulcherrima", "pulcherrimus", "pulcherrimum")} == \
        {"pulcherrim"}  # fmt: skip
    assert {epithet_stem(e) for e in ("niger", "nigra", "nigrum")} == {"nig"}
    assert epithet_stem("smithii") == "smithii"
    assert author_key("H. Karst.") == "hkarst" and author_key("Linnaeus") == "l"
    assert author_key("Hook. & Arn.") == author_key("Hook. et Arn.") == "hook&arn"


def features_from(source: str, **attributes) -> dict[str, list[str]]:
    view: dict[str, list[str]] = {}
    record = SimpleNamespace(attributes=attributes, source=SimpleNamespace(type=source))
    for normalizer in taxon().normalizers:
        for f in normalizer.normalize(record):
            view.setdefault(f.feature, []).append(f.value)
    return view


def features(**attributes) -> dict[str, list[str]]:
    return features_from("field", **attributes)


NAME, AUTHORS = TaxonNameComparison(), AuthorshipComparison()


@pytest.mark.parametrize(
    ("a", "b", "level"),
    [
        ("Caesalpinia pulcherrima", "CAESALPINIA PULCHERRIMA Sw.", "exact"),
        ("Acer rubrum", "Acer ruber", "gender_variant"),
        ("C. pulcherrima", "Caesalpinia pulcherrima", "abbreviated_genus"),
        ("Caesalpinia pulcherima", "Caesalpinia pulcherrima", "fuzzy"),
        ("Poinciana pulcherrima L.", "Caesalpinia pulcherrima (L.) Sw.", "recombination"),
        ("Caesalpinia gilliesii", "Caesalpinia pulcherrima", "same_genus"),
        ("Picea abies", "Abies alba", "different"),
    ],
)
def test_name_levels(a, b, level):
    assert NAME.levels[NAME.compare(features(name=a), features(name=b))] == level
    assert NAME.compare(features(name="Quercus sp."), features(name="Quercus robur")) is None


@pytest.mark.parametrize(
    ("a", "b", "level"),
    [
        ("Picea abies (L.) H.Karst.", "Picea abies (L.) H. Karst.", "same"),
        ("Poinciana pulcherrima L.", "Caesalpinia pulcherrima (L.) Sw.", "basionym_match"),
        ("Picea abies (L.) Karst.", "Picea abies H.Karst.", "compatible"),
        ("Aster alpinus L.", "Aster alpinus Thunb.", "homonym"),
        ("Aster alpinus L.", "Aster amellus Thunb.", "different"),
    ],
)
def test_authorship_levels(a, b, level):
    assert AUTHORS.levels[AUTHORS.compare(features(name=a), features(name=b))] == level


def test_assertions_and_kingdoms():
    a = AssertionComparison()
    synonym = features(name="Poinciana pulcherrima L.",
                       accepted_name="Caesalpinia pulcherrima (L.) Sw.")  # fmt: skip
    accepted = features(name="Caesalpinia pulcherrima (L.) Sw.", taxonomic_status="accepted")
    other = features(name="Pinus picea L.", accepted_name="Abies alba Mill.")
    assert a.levels[a.compare(synonym, accepted)] == "asserted_same"
    assert a.levels[a.compare(synonym, features(name="Caesalpinia pulcherrima"))] == "asserted_same"
    assert a.levels[a.compare(synonym, other)] == "asserted_different"
    # The reference authority's assertion is its own level, and decisive.
    reference = features_from("wfo", name="Pinus picea L.", taxonomic_status="synonym",
                              accepted_name="Abies alba Mill.")  # fmt: skip
    assert a.levels[a.compare(reference, features(name="Abies alba Mill."))] == "reference_same"
    assert AcceptedByReference().apply({"assertion": "reference_same"}) == "MATCH"
    # WFO accepting Fagus sinensis says nothing about a name WFO does not list under it:
    # another checklist sinking Fagus longipetiolata there is only that checklist's word.
    wfo_sinensis = features_from("wfo", name="Fagus sinensis Oliv.", taxonomic_status="accepted")
    col_sunk = features_from("col", name="Fagus longipetiolata Seemen",
                             taxonomic_status="synonym", accepted_name="Fagus sinensis Oliv.")  # fmt: skip
    assert a.levels[a.compare(wfo_sinensis, col_sunk)] == "asserted_same"
    col_same = features_from("col", name="Fagus sinensis Oliv.", taxonomic_status="accepted")
    assert a.levels[a.compare(wfo_sinensis, col_same)] == "reference_same"
    assert a.compare(features(name="Rosa canina"), features(name="Rosa canina")) is None
    plant, bird = features(name="Oenanthe crocata", kingdom="Plantae"), \
        features(name="Oenanthe oenanthe", kingdom="Animalia")  # fmt: skip
    assert KingdomConflict().cannot_link(plant, bird)


def obs(name, source, **attributes):
    return SimpleNamespace(observation_id=f"obs_{name}", entity_type="taxon",
                           source=SimpleNamespace(type=source),
                           attributes={"name": name, **attributes})  # fmt: skip


def test_the_canonical_name_is_the_authoritys_and_says_so():
    canon = AcceptedByAuthority("wfo")
    accepted = obs("Caesalpinia pulcherrima (L.) Sw.", "wfo", taxonomic_status="accepted")
    synonym = obs("Poinciana pulcherrima L.", "wfo", taxonomic_status="synonym",
                  accepted_name="Caesalpinia pulcherrima (L.) Sw.")  # fmt: skip
    popular = [obs(f"Poinciana pulcherrima {i}", "trade") for i in range(5)]
    built = canon.build([*popular, synonym])
    assert built["name"] == "Caesalpinia pulcherrima (L.) Sw."
    assert built["accepted_according_to"] == "wfo"
    assert canon.build([accepted, *popular])["name"] == "Caesalpinia pulcherrima (L.) Sw."
    assert "accepted_according_to" not in canon.build(popular)  # no authority: most frequent
    picker = ByProfile({"taxon": taxon()})
    assert picker.pick([accepted]) is not picker.default
    assert isinstance(ByProfile({}).pick([accepted]), MostFrequentValue)


def test_the_fields_describe_the_accepted_name_not_its_synonyms():
    """Synonyms outnumber their accepted name; a majority would describe them."""
    canon = AcceptedByAuthority("wfo")
    synonyms = [obs(f"Castanea americana var. v{i}", "wfo", authorship="Marshall", rank="variety",
                    taxonomic_status="synonym", family="Fagaceae",
                    accepted_name="Castanea dentata Borkh.") for i in range(4)]  # fmt: skip
    accepted = obs("Castanea dentata", "wfo", authorship="Borkh.", rank="species",
                   taxonomic_status="accepted", family="Fagaceae")  # fmt: skip
    built = canon.build([*synonyms, accepted])
    assert built["name"] == "Castanea dentata Borkh."
    assert (built["authorship"], built["rank"], built["taxonomic_status"]) == \
        ("Borkh.", "species", "accepted")  # fmt: skip
    assert "accepted_name" not in built
    only_synonyms = canon.build(synonyms)
    assert only_synonyms["name"] == "Castanea dentata Borkh."
    assert only_synonyms["family"] == "Fagaceae"  # shared by the taxon's names: kept
    for field in ("authorship", "rank", "taxonomic_status", "accepted_name"):
        assert field not in only_synonyms  # each describes one synonym, not the taxon


def hand_model(profile, weights: dict[str, dict[str, float]]) -> FSModel:
    """A model with fixed level weights: u = 1 and m = 2 ** weight for every level."""
    levels = {c.name: c.levels for c in profile.comparisons}
    return FSModel(
        entity_type=profile.entity_type, comparisons=tuple(levels), levels=levels,
        m={c: tuple(2 ** weights.get(c, {}).get(level, 0.0) for level in ls)
           for c, ls in levels.items()},
        u={c: tuple(1.0 for _ in ls) for c, ls in levels.items()},
        lam=0.2, version="test",
    )  # fmt: skip


WEIGHTS = {
    "name": {"exact": 9, "gender_variant": 8, "abbreviated_genus": 4, "fuzzy": 6,
             "recombination": 2, "same_genus": 0, "different": -9},
    "authorship": {"same": 4, "basionym_match": 5, "compatible": 2, "homonym": -8,
                   "different": -2},
    "rank": {"same": 0.5, "different": -4},
    "assertion": {"reference_same": 14, "asserted_same": 8, "asserted_different": -10},
    "vernacular": {"exact": 2},
    "family": {"same": 1, "different": -8},
    "ident": {"exact": 15},
}  # fmt: skip


def test_the_peacock_flowers_names_resolve_to_one_taxon(store, ids, now):
    profile = taxon()
    engine = Thisness(store, profiles=[profile], ids=ids, clock=lambda: now)
    model = hand_model(profile, WEIGHTS)
    store.set_meta("model.taxon", register_model(store, model))
    store.set_meta("model.taxon.source", "fitted")
    rows = [
        ({"name": "Caesalpinia pulcherrima (L.) Sw.", "taxonomic_status": "accepted"}, "wfo"),
        ({"name": "Poinciana pulcherrima L.", "taxonomic_status": "synonym",
          "accepted_name": "Caesalpinia pulcherrima (L.) Sw."}, "wfo"),
        ({"name": "Caesalpinia pulcherrima", "vernacular_names": ["en:Barbados pride"]}, "trade"),
        ({"name": "CAESALPINIA PULCHERRIMA SW."}, "herbarium"),
        ({"name": "Caesalpinia gilliesii (Hook.) D.Dietr.", "taxonomic_status": "accepted"}, "wfo"),
        ({"name": "Pinus picea L.", "taxonomic_status": "synonym",
          "accepted_name": "Abies alba Mill."}, "wfo"),
        ({"name": "Abies alba Mill.", "taxonomic_status": "accepted"}, "wfo"),
        ({"name": "Picea abies (L.) H.Karst.", "taxonomic_status": "accepted"}, "wfo"),
    ]  # fmt: skip
    observed = [engine.observe("taxon", a, {"type": s, "id": str(i)})
                for i, (a, s) in enumerate(rows)]  # fmt: skip
    engine.resolve_many("taxon", observed)
    entity = [engine.entity_of(o.observation_id) for o in observed]
    assert len(set(entity[:4])) == 1  # the peacock flower, under four names
    assert entity[4] not in entity[:4]  # its sister species stays apart
    assert entity[5] == entity[6] != entity[7]  # Pinus picea is Abies alba, not Picea abies
    snapshot = engine.get_entity(entity[0])[1]
    assert snapshot.attributes["name"] == "Caesalpinia pulcherrima (L.) Sw."
    assert snapshot.attributes["accepted_according_to"] == "wfo"
    assert snapshot.canonicalizer_ref == profile.canonicalizer.ref.ref
    assert engine.dossier(entity[0]).label == "Caesalpinia pulcherrima (L.) Sw."
    assert check(store) == []


def test_authorities_that_disagree_go_to_review(store, ids, now):
    """WFO treats Pinus picea L. as a synonym of Abies alba; a second checklist lists
    both as accepted. The reference decides the entity; the dissent is not dropped."""
    profile = taxon()
    engine = Thisness(store, profiles=[profile], ids=ids, clock=lambda: now)
    store.set_meta("model.taxon", register_model(store, hand_model(profile, WEIGHTS)))
    store.set_meta("model.taxon.source", "fitted")
    rows = [
        ({"name": "Pinus picea L.", "taxonomic_status": "synonym",
          "accepted_name": "Abies alba Mill."}, "wfo"),
        ({"name": "Abies alba Mill.", "taxonomic_status": "accepted"}, "wfo"),
        ({"name": "Pinus picea L.", "taxonomic_status": "accepted"}, "col"),
        ({"name": "Abies alba Mill.", "taxonomic_status": "accepted"}, "col"),
    ]  # fmt: skip
    observed = [engine.observe("taxon", a, {"type": s, "id": str(i)})
                for i, (a, s) in enumerate(rows)]  # fmt: skip
    engine.resolve_many("taxon", observed)
    entity = [engine.entity_of(o.observation_id) for o in observed]
    assert entity[0] == entity[1] == entity[3]  # WFO's taxon, which the second list's row joins
    assert entity[2] != entity[0]  # the dissenting row is not merged in silently
    in_review = {m for s in engine.reviews() for m in s.review.members}
    assert observed[2].observation_id in in_review or entity[2] in in_review
    assert check(store) == []

    # The dossier says which authority said what (design 06 §3).
    d = engine.dossier(entity[0])
    views = {(v.source, v.accepted, v.agrees, v.reference) for v in d.authorities}
    assert ("wfo", "Abies alba Mill.", True, True) in views
    assert ("col", "Abies alba Mill.", True, False) in views
    assert d.authorities[0].reference  # the reference first
    assert "## Authorities" in d.to_context() and "wfo (reference)" in d.to_context()
    dissent = engine.dossier(entity[2])
    assert [(v.source, v.accepted, v.agrees) for v in dissent.authorities] == \
        [("col", "Pinus picea L.", True)]  # fmt: skip


def test_a_checklist_sinking_a_name_the_reference_accepts_is_not_the_reference(store, ids, now):
    """CoL sinks Fagus longipetiolata into F. sinensis; WFO accepts both. WFO's word
    about F. sinensis is not WFO's word about F. longipetiolata, so the rule that makes
    the reference decisive must not fire, and CoL's record is not merged on CoL's say."""
    profile = taxon()
    engine = Thisness(store, profiles=[profile], ids=ids, clock=lambda: now)
    store.set_meta("model.taxon", register_model(store, hand_model(profile, WEIGHTS)))
    store.set_meta("model.taxon.source", "fitted")
    rows = [
        ({"name": "Fagus sinensis", "authorship": "Oliv.", "taxonomic_status": "accepted"}, "wfo"),
        ({"name": "Fagus longipetiolata", "authorship": "Seemen",
          "taxonomic_status": "accepted"}, "wfo"),
        ({"name": "Fagus longipetiolata", "authorship": "Seemen", "taxonomic_status": "synonym",
          "accepted_name": "Fagus sinensis Oliv."}, "col"),
    ]  # fmt: skip
    observed = [engine.observe("taxon", a, {"type": s, "id": str(i)})
                for i, (a, s) in enumerate(rows)]  # fmt: skip
    engine.resolve_many("taxon", observed)
    entity = [engine.entity_of(o.observation_id) for o in observed]
    assert entity[0] != entity[1]  # WFO's two taxa
    assert entity[2] != entity[0]  # CoL's word alone does not move the name
    views = [v for v in engine.dossier(entity[2]).authorities if v.source == "col"]
    assert views and views[0].accepted == "Fagus sinensis Oliv." and not views[0].agrees
    assert check(store) == []


# -- the WFO benchmark (design 06 §5) -----------------------------------------------------------


def test_the_shipped_fixture_is_wfo():
    from thisness.benchmark import taxa

    rows = taxa.fixture()
    assert {r.family for r in rows} == set(taxa.FIXTURE_FAMILIES)
    assert {r.status for r in rows} == {"accepted", "synonym"}
    by_id = {r.id: r for r in rows}
    assert all(r.accepted_id in by_id for r in rows)  # every synonym's taxon is in the fixture
    assert all(by_id[r.accepted_id].status == "accepted" for r in rows)
    assert any(r.name == "Picea abies" and r.status == "accepted" for r in rows)


def test_benchmark_generation_is_deterministic_and_keeps_wfo_truth():
    from thisness.benchmark import taxa

    a, b = taxa.from_fixture(100, seed=3), taxa.from_fixture(100, seed=3)
    assert [r.attributes for r in a.records] == [r.attributes for r in b.records]
    assert set(a.kind.values()) <= {"homonym", "infraspecific", "synonymized", "plain"}
    accepted = {r.id for r in a.rows if r.status == "accepted"}
    assert set(a.truth.values()) <= accepted
    assert {r.source for r in a.records} <= set(taxa.SOURCES)

    ref = taxa.from_fixture(100, seed=3, reference=True)
    wfo = [r for r in ref.records if r.source == "wfo"]
    assert wfo and all(r.attributes["identifiers"][0].startswith("WFO:wfo-") for r in wfo)
    synonyms = [r for r in wfo if r.attributes["taxonomic_status"] == "synonym"]
    assert synonyms and all("accepted_name" in r.attributes for r in synonyms)


def test_author_spellings_replace_whole_authors_only():
    import random

    from thisness.benchmark import taxa

    row = taxa.NameRow("x", "Populus yatungensis", "(Z.Wang & P.Y.Fu) Z.Wang & S.L.Tung",
                       "species", "accepted", "x", "Salicaceae", "Populus")  # fmt: skip
    rng = random.Random(0)
    for _ in range(300):
        assert "Linn" not in taxa._variant(row, rng) and "linn" not in taxa._variant(row, rng)


def test_shipped_taxon_starter_matches_the_profile():
    import json
    from importlib import resources

    from thisness.starter import load_starter

    profile = taxon()
    starter = load_starter(profile)
    assert starter is not None and starter.unlearned == ()
    assert starter.comparisons == tuple(c.name for c in profile.comparisons)
    payload = json.loads(
        (resources.files("thisness.profiles") / "data" / profile.starter_model).read_text()
    )
    assert payload["trained_on"]["generator"] == "wfo"
    assert not set(payload["trained_on"]["families"]) & {"Pinaceae", "Betulaceae", "Juglandaceae",
                                                          "Fagaceae", "Sapindaceae"}  # fmt: skip


def test_a_homonym_is_capped_below_zero():
    """One name with two authors is two names: EM cannot see that from random pairs,
    so the profile states it (design 06 §10.2)."""
    from thisness.starter import load_starter

    model = load_starter(taxon())
    levels = model.levels["authorship"]
    assert model.level_weight("authorship", levels.index("homonym")) <= -8.0
    assert model.level_weight("authorship", levels.index("homonym"), capped=False) > 0


def test_taxon_benchmark_regression():
    """The taxon quality floor on the shipped fixture. Lowering these needs a reason."""
    from thisness.benchmark import taxa
    from thisness.benchmark.evaluate import run_benchmark

    report, engine, _ = run_benchmark(taxa.from_fixture(300), random_pairs=100_000)
    c = report.clusters
    model = engine.model("taxon")
    # Unaided, every name generator blocks on the name: the starter supplies its m.
    assert model.sessions["borrowed_from_prior"]["comparisons"] == ["name"]
    assert c.b3_precision >= 0.997  # 0.9986 when written
    assert c.false_merge_clusters <= 2  # 1
    assert c.b3_recall >= 0.45  # 0.501: heterotypic synonyms are unreachable without a reference

    report, engine, _ = run_benchmark(taxa.from_fixture(300, reference=True),
                                      random_pairs=100_000)  # fmt: skip
    c = report.clusters
    assert engine.model("taxon").unlearned == ()
    assert "borrowed_from_prior" not in engine.model("taxon").sessions
    assert c.b3_precision >= 0.996  # 0.9978
    assert c.false_merge_clusters <= 4  # 3, all homonyms
    assert c.b3_recall >= 0.80  # 0.827
    assert c.by_kind_recall["synonymized"] >= 0.70  # 0.754


def col_row(id, name, authorship, status, accepted_name="", family="Pinaceae"):
    from thisness.benchmark import taxa

    return taxa.NameRow(id, name, authorship, "species", status, id, family,
                        name.split()[0], accepted_name)  # fmt: skip


def test_a_second_authority_is_aligned_by_name():
    from thisness.benchmark import taxa

    wfo = [col_row("w1", "Castanopsis inermis", "(Lindl.) Benth. & Hook.f.", "accepted"),
           col_row("w2", "Quercus ilex", "L.", "accepted"),
           col_row("w3", "Quercus ilex", "Lour.", "synonym")]  # fmt: skip
    col = [col_row("c1", "Castanopsis inermis", "(Lindl. ex Wall.) Benth. & Hook.fil.", "accepted"),
           col_row("c2", "Quercus ilex", "L.", "accepted"),
           col_row("c3", "Quercus ilex", "", "accepted"),  # ambiguous without its author
           col_row("c4", "Quercus nowhere", "Nobody", "accepted")]  # fmt: skip
    found = taxa.align(col, wfo)
    assert {k: v.id for k, v in found.items()} == {"c1": "w1", "c2": "w2"}


def test_dissent_is_measured_against_the_reference():
    from thisness.benchmark import taxa

    rows = [r for r in taxa.fixture() if r.genus == "Abies"]
    accepted = sorted((r for r in rows if r.status == "accepted" and r.rank == "species"),
                      key=lambda r: r.id)  # fmt: skip
    one, other = accepted[0], accepted[1]
    one_full = f"{one.name} {one.authorship}".strip()
    other_full = f"{other.name} {other.authorship}".strip()
    col = [
        col_row("c1", one.name, one.authorship, "accepted", one_full, one.family),
        # CoL sinks the second species into the first: dissent by WFO's lights.
        col_row("c2", other.name, other.authorship, "synonym", one_full, other.family),
    ]
    every = sum(r.status == "accepted" for r in rows)  # the whole genus, so both are sampled
    bench = taxa.generate(rows, n_taxa=every, seed=1, families=("Pinaceae",),
                          reference=True, col=col)  # fmt: skip
    col_records = [r for r in bench.records if r.source == "col"]
    assert len(col_records) == 2
    assert {r.attributes["identifiers"][0] for r in col_records} == {"COL:c1", "COL:c2"}
    sunk = next(r for r in col_records if r.attributes["name"] == other.name)
    assert bench.dissent == {sunk.record_id: "placement"}
    assert bench.truth[sunk.record_id] == other.id  # WFO's truth: the name's own taxon
    assert bench.kind[other.id] == "disputed"
    assert bench.config["authorities"] == ["wfo", "col"]
    assert other_full not in {r.attributes.get("accepted_name") for r in col_records}
