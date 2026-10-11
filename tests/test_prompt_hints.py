"""Per-profile prompt guidance, and the model layer on taxa (design 06 §6, §10.5)."""

from __future__ import annotations

from types import SimpleNamespace

from thisness.adjudicate import Adjudicator
from thisness.adjudicate import default_prompt as adjudicate_prompt
from thisness.assistant import default_prompt as review_prompt
from thisness.benchmark import taxa
from thisness.benchmark.evaluate import run_benchmark
from thisness.benchmark.oracle import benchmark_truth, oracle_provider
from thisness.edges import FellegiSunterEdgePolicy
from thisness.llm import LLMClient, LLMPolicy
from thisness.name_only import NameOnlyPlan
from thisness.name_only import default_prompt as group_prompt
from thisness.normalize.taxon import TaxonName, collapse_key
from thisness.profiles.organization import organization
from thisness.profiles.person import person
from thisness.profiles.taxon import taxon
from thisness.storage.duckdb.invariants import check

PROMPTS = (adjudicate_prompt, review_prompt, group_prompt)


def test_guidance_is_part_of_the_prompt_and_its_ref():
    for make in PROMPTS:
        base = make()
        hinted = base.for_profile(taxon())
        assert "Guidance for taxon records:" in hinted.system and "basionym" in hinted.system
        assert hinted.ref != base.ref and hinted.ref.name == base.ref.name
        assert base.with_hints("taxon", None) is base and base.for_profile(None) is base
        other = base.with_hints("taxon", "- something else")
        assert other.ref != hinted.ref  # changing the guidance is a new prompt


def test_the_shared_prompts_speak_of_no_domain():
    for make in PROMPTS:
        text = make().system.casefold()
        for word in ("harjas", "household", "company", "companies", "legal-form", "birth"):
            assert word not in text, (make().name, word)
    assert "Harjas" in organization().prompt_hints
    assert "household" in person().prompt_hints
    assert "recombination" in taxon().prompt_hints.casefold()


def view(name: str) -> dict[str, list[str]]:
    record = SimpleNamespace(attributes={"name": name}, source=SimpleNamespace(type="field"))
    out: dict[str, list[str]] = {}
    for f in TaxonName().normalize(record):
        out.setdefault(f.feature, []).append(f.value)
    return out


def test_taxon_names_collapse_on_name_and_author_only():
    assert collapse_key(view("Quercus ilex L.")) == collapse_key(view("QUERCUS ILEX L."))
    keys = {
        collapse_key(view(n)) for n in ("Quercus ilex", "Quercus ilex L.", "Quercus ilex Lour.")
    }
    assert len(keys) == 3  # an authorless name may be either: the model judges it
    assert collapse_key(view("Quercus sp.")) is None
    assert taxon().collapse_key is collapse_key and organization().collapse_key is None


def test_a_profile_sets_its_own_negative_threshold():
    policy = FellegiSunterEdgePolicy()
    row = {"cannot_link": False, "rule_verdict": None, "weight": -0.1}
    assert policy.classify(row, 12.0) == "negative"
    assert policy.classify(row, 12.0, taxon().max_negative_weight) == "contested"
    assert policy.classify({**row, "weight": -6.0}, 12.0, -4.0) == "negative"
    assert organization().max_negative_weight is None


def related(bench):
    return lambda a, b: bench.group.get(a) is not None and bench.group.get(a) == bench.group.get(b)


def oracle_client(engine, truth, bench, **errors):
    provider = oracle_provider(benchmark_truth(engine, truth), related(bench), **errors)
    return LLMClient(engine.store, provider, ids=engine.ids, clock=engine.clock,
                     policy=LLMPolicy(concurrency=1))  # fmt: skip


def test_the_adjudicator_sends_the_taxon_guidance_and_resolves_more():
    bench = taxa.from_fixture(200)
    baseline, _, _ = run_benchmark(bench, random_pairs=60_000)

    def adjudicator(engine, truth, b):
        return Adjudicator(oracle_client(engine, truth, b), model="oracle", mode="decide",
                           max_pairs=None)  # fmt: skip

    report, engine, _ = run_benchmark(bench, random_pairs=60_000, adjudicator=adjudicator)
    expected = adjudicate_prompt().for_profile(taxon()).ref.ref
    invocations = engine.invocations()
    assert invocations and {i.prompt_ref for i in invocations} == {expected}
    assert report.clusters.b3_recall > baseline.clusters.b3_recall + 0.1
    assert report.clusters.false_merge_clusters <= baseline.clusters.false_merge_clusters + 1
    assert check(engine.store) == []


def test_name_only_groups_plant_names():
    bench = taxa.from_fixture(150)
    baseline, _, _ = run_benchmark(bench, random_pairs=60_000)

    def plan(engine, truth, b):
        return NameOnlyPlan(oracle_client(engine, truth, b, local=False))

    report, engine, _ = run_benchmark(bench, random_pairs=60_000, plan=plan)
    assert {i.prompt_ref for i in engine.invocations()} == \
        {group_prompt().for_profile(taxon()).ref.ref}  # fmt: skip
    assert report.run["collapsed_groups"] > 0
    assert report.clusters.b3_recall > baseline.clusters.b3_recall + 0.3
    assert report.clusters.b3_precision >= 0.99
    assert check(engine.store) == []
