from __future__ import annotations

import json
from collections import Counter
from importlib import resources

import pytest

from thisness.benchmark.evaluate import candidate_metrics, cluster_metrics, run_benchmark
from thisness.benchmark.organizations import generate
from thisness.profiles.organization import organization
from thisness.starter import StarterMismatch, load_starter


def test_generator_is_deterministic_and_scales():
    a, b = generate(n_orgs=200, seed=3), generate(n_orgs=200, seed=3)
    assert [r.attributes for r in a.records] == [r.attributes for r in b.records]
    assert generate(n_orgs=200, seed=4).records[0].attributes != a.records[0].attributes
    kinds = Counter(a.kind.values())
    assert set(kinds) == {"normal", "common", "heavy", "family", "acronym"}
    assert len(generate(n_orgs=1000, seed=3).organizations) == pytest.approx(1000, abs=5)


def test_group_companies_share_a_brand_but_are_distinct():
    bench = generate(n_orgs=400, seed=3)
    groups: dict[str, list] = {}
    for org in bench.organizations:
        if org.group:
            groups.setdefault(org.group, []).append(org)
    assert groups and all(len(members) == 3 for members in groups.values())
    for members in groups.values():
        assert len({m.tokens[0] for m in members}) == 1
        assert len({m.org_id for m in members}) == 3


def test_cluster_metrics():
    truth = {"a": "A", "b": "A", "c": "B", "d": "B", "e": "C"}
    kind = {"A": "normal", "B": "common", "C": "common"}
    perfect = cluster_metrics({"a": 1, "b": 1, "c": 2, "d": 2, "e": 3}, truth, kind)
    assert (perfect.b3_precision, perfect.b3_recall, perfect.false_merge_clusters) == (1, 1, 0)
    merged = cluster_metrics({"a": 1, "b": 1, "c": 2, "d": 2, "e": 2}, truth, kind)
    assert merged.false_merge_clusters == 1 and merged.contaminated == 1
    assert merged.largest_bad_cluster == 3 and merged.mixed_by_kind == {"common": 1}
    split = cluster_metrics({"a": 1, "b": 2, "c": 3, "d": 3, "e": 4}, truth, kind)
    assert split.b3_precision == 1 and split.b3_recall < 1


def test_candidate_metrics():
    truth = {"a": "A", "b": "A", "c": "A", "d": "B"}
    m = candidate_metrics({("a", "b"), ("b", "c"), ("a", "d")}, truth, {"A": "x", "B": "y"})
    assert m["pair_recall"] == pytest.approx(2 / 3)
    assert m["entity_connectivity"] == 1.0  # a-b-c connected through b


def test_shipped_starter_matches_the_profile():
    profile = organization()
    starter = load_starter(profile)
    assert starter is not None and starter.unlearned == ()
    assert starter.comparisons == tuple(c.name for c in profile.comparisons)
    payload = json.loads(
        (resources.files("thisness.profiles") / "data" / profile.starter_model).read_text()
    )
    assert payload["trained_on"]["generator"] == "organizations"


def test_a_stale_starter_is_refused(monkeypatch):
    import thisness.starter as starter_module

    monkeypatch.setattr(starter_module, "_comparisons", lambda profile: ["comparison:other@1#x"])
    with pytest.raises(StarterMismatch, match="Rebuild it"):
        load_starter(organization())


def test_benchmark_regression():
    """The end-to-end quality floor on a small benchmark (design 03 §21). A change
    that lowers these numbers needs a reason, not a new threshold."""
    report, engine, _ = run_benchmark(generate(n_orgs=400, seed=11), random_pairs=100_000)
    c = report.clusters
    assert engine.model_source("organization") == "fitted"
    assert report.candidates["pair_recall"] >= 0.97
    assert report.candidates["entity_connectivity"] >= 0.98
    assert c.b3_precision >= 0.999
    assert c.false_merge_clusters <= 1
    assert c.b3_recall >= 0.75  # 0.790 when written
    # 0.448: a heavy supplier's records that drop or add a word now read as a group's
    # other company, as they mostly are on real UK data (design 07 §13.2); was 0.615.
    # Overall recall at 805 organizations rose (.888 to .894).
    assert c.by_kind_recall["heavy"] >= 0.42
    assert c.mixed_by_kind.get("family", 0) == 0  # group companies never merged


# -- people ------------------------------------------------------------------------------------


def test_people_generator_has_every_trap():
    from thisness.benchmark.people import KINDS
    from thisness.benchmark.people import generate as people

    bench = people(n_people=1000, seed=3)
    assert [r.attributes for r in bench.records[:50]] == \
        [r.attributes for r in people(n_people=1000, seed=3).records[:50]]  # fmt: skip
    assert set(bench.kind.values()) == set(KINDS)
    households: dict[str, list] = {}
    for p in bench.people:
        if p.household:
            households.setdefault(p.household, []).append(p)
    for household, members in households.items():
        assert len({m.family for m in members}) == 1 and len({m.address for m in members}) == 1
        if household.startswith("h"):  # fathers and sons share a given name on purpose
            assert len({m.given for m in members}) == len(members)
    mailboxes = [(p.personal_email.split("@")[0].replace(".", ""), p.personal_email.split("@")[1])
                 for p in bench.people]  # fmt: skip
    assert len(mailboxes) == len(set(mailboxes))  # one mailbox, one person


def test_shipped_person_starter_matches_the_profile():
    from thisness.profiles.person import person

    profile = person()
    starter = load_starter(profile)
    assert starter is not None and starter.unlearned == ()
    assert starter.comparisons == tuple(c.name for c in profile.comparisons)


def test_person_benchmark_regression():
    """The Person quality floor (design 04 §9.4). Same rule: lowering these needs a reason."""
    from thisness.benchmark.people import generate as people

    report, engine, _ = run_benchmark(people(n_people=1000, seed=11), random_pairs=100_000)
    c = report.clusters
    assert engine.model_source("person") == "fitted"
    assert report.run["term_frequency"] == "observations"
    assert report.candidates["pair_recall"] >= 0.92
    assert c.b3_precision >= 0.997  # 0.9983 when written
    assert c.false_merge_clusters <= 6  # 4: one common name, three fathers and sons
    assert c.b3_recall >= 0.70  # 0.728
    for kind in ("household", "twin", "recycled", "married"):
        assert c.mixed_by_kind.get(kind, 0) == 0, kind
    model = engine.model("person")
    # Shared by related people: capped, and contradicting name levels below zero.
    assert model.level_weight("phone", 0) <= 4.0 and model.level_weight("address", 0) <= 3.0
    assert model.level_weight("name", model.levels["name"].index("family_only")) <= -6.0
    candidates = engine.relationship_candidates()
    assert any("household" in c.signals for c in candidates)
