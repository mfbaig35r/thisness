from __future__ import annotations

import json
from dataclasses import replace

import pytest

from thisness.adjudicate import Adjudicator
from thisness.benchmark import organizations, people
from thisness.benchmark.evaluate import run_benchmark
from thisness.benchmark.oracle import benchmark_truth, oracle_provider
from thisness.llm import LLMClient, LLMPolicy, ProviderError
from thisness.llm.providers import FunctionProvider
from thisness.storage.duckdb.invariants import check


def related(bench):
    return lambda a, b: bench.group.get(a) is not None and bench.group.get(a) == bench.group.get(b)


def oracle(mode="shadow", policy=None, **errors):
    def build(engine, truth, bench):
        provider = oracle_provider(benchmark_truth(engine, truth), related(bench), **errors)
        client = LLMClient(engine.store, provider, ids=engine.ids, clock=engine.clock,
                           policy=policy or LLMPolicy(concurrency=1))  # fmt: skip
        return Adjudicator(client, model="oracle", mode=mode, max_pairs=None)

    return build


def provider_from(fn):
    """An adjudicator factory whose model answers with ``fn(pairs)``."""

    def build(engine, truth, bench):
        def answer(request):
            return fn(json.loads(request.messages[0].content)["pairs"])

        client = LLMClient(engine.store, FunctionProvider(answer), ids=engine.ids,
                           clock=engine.clock, policy=LLMPolicy(concurrency=1))  # fmt: skip
        return Adjudicator(client, model="fake", mode="decide", max_pairs=None)

    return build


def partition(assignment):
    """Groups of observations, independent of entity ids."""
    groups = {}
    for obs, entity in assignment.items():
        groups.setdefault(entity, set()).add(obs)
    return {frozenset(g) for g in groups.values()}


def bench(n=300):
    return organizations.generate(n, seed=11)


@pytest.fixture(scope="module")
def baseline():
    report, engine, truth = run_benchmark(bench(), random_pairs=60_000)
    return report, partition(engine.store.entities_of(list(truth)))


def test_shadow_records_answers_and_changes_nothing(baseline):
    report, engine, truth = run_benchmark(bench(), random_pairs=60_000, adjudicator=oracle())
    assert partition(engine.store.entities_of(list(truth))) == baseline[1]
    stats = report.run["adjudication"]
    assert stats["mode"] == "shadow" and stats["applied"] == 0 and stats["answered"] > 100
    judgments = [d for d in engine.store.decisions_of_type("pair") if d.actor.kind == "agent"]
    assert len(judgments) == stats["answered"]
    sample = judgments[0]
    assert sample.invocation_id and sample.evidence_ids
    explained = engine.explain(sample.decision_id)
    assert explained["invocation"]["status"] in ("ok", "cached")
    kinds = {e["kind"] for e in explained["evidence"]}
    assert "llm_adjudication" in kinds and any(k.startswith("compare.") for k in kinds)
    # A right model is scored right against the truth.
    vs = stats["vs_truth"]
    assert vs["judged"] == stats["answered"] and vs["accuracy"] == 1.0
    assert vs["wrongly_apart"] in (0.0, None) and vs["wrongly_same"] in (0.0, None)
    assert check(engine.store) == []


def test_accuracy_counts_a_wrong_model_s_errors():
    report, _, _ = run_benchmark(bench(), random_pairs=60_000, adjudicator=oracle(error=0.2))
    vs = report.run["adjudication"]["vs_truth"]
    assert 0.65 < vs["accuracy"] < 0.92  # about 0.8
    assert vs["wrongly_apart"] > 0.05 and vs["wrongly_same"] > 0.05
    assert sum(vs["confusion"].values()) == vs["judged"]


def test_decide_with_a_right_model_resolves_more_and_merges_no_more(baseline):
    report, engine, _ = run_benchmark(bench(), random_pairs=60_000, adjudicator=oracle("decide"))
    before, after = baseline[0].clusters, report.clusters
    assert after.false_merge_clusters <= before.false_merge_clusters
    assert after.b3_recall > before.b3_recall + 0.05
    assert report.reviews_opened < baseline[0].reviews_opened
    # Consensus decisions cite the model's judgments behind the edges it set.
    agent = {d.decision_id for d in engine.store.decisions_of_type("pair")
             if d.actor.kind == "agent"}  # fmt: skip
    cited = {e for d in engine.store.decisions_of_type("consensus")
             for e in d.flags.get("edges", ())}  # fmt: skip
    assert agent & cited
    assert check(engine.store) == []


def test_a_weak_pair_is_not_merged_on_a_models_word():
    def always_same(pairs):
        return {"answers": [{"pair": p["pair"], "decision": "same"} for p in pairs]}

    report, engine, _ = run_benchmark(bench(), random_pairs=60_000,
                                      adjudicator=provider_from(always_same))  # fmt: skip
    applied = [d for d in engine.store.decisions_of_type("pair") if d.actor.kind == "agent"
               and dict((s.kind, s.value) for s in d.scores)["llm_applied"]]  # fmt: skip
    stats = report.run["adjudication"]
    assert 0 < stats["applied"] < stats["answered"]  # the 6-bit floor held some back
    assert len(applied) == stats["applied"]
    assert check(engine.store) == []


def test_bad_answers_are_discarded_not_trusted():
    def bad(pairs):
        answers = []
        for i, p in enumerate(pairs):
            if i % 3 == 0:
                answers.append({"pair": p["pair"], "decision": "same", "supporting": ["E999"]})
            elif i % 3 == 1:
                answers.append({"pair": p["pair"], "decision": "related",
                                "relation": "married_to"})  # fmt: skip
        return {"answers": answers}  # every third pair left unanswered

    report, _, _ = run_benchmark(bench(), random_pairs=60_000, adjudicator=provider_from(bad))
    stats = report.run["adjudication"]
    assert stats["answered"] == 0 and stats["applied"] == 0
    assert set(stats["unanswered"]) == {"invalid_citation", "unknown_relation", "missing"}


def test_failures_and_budgets_leave_pairs_to_review(baseline):
    def down(engine, truth, bench):
        def fail(request):
            raise ProviderError("overloaded", retryable=True)

        client = LLMClient(engine.store, FunctionProvider(fail), ids=engine.ids,
                           clock=engine.clock, policy=LLMPolicy(concurrency=1))  # fmt: skip
        return Adjudicator(client, model="fake", mode="decide", max_pairs=None)

    report, engine, truth = run_benchmark(bench(), random_pairs=60_000, adjudicator=down)
    assert report.run["adjudication"]["unanswered"].keys() == {"failed"}
    assert partition(engine.store.entities_of(list(truth))) == baseline[1]
    capped = oracle("decide", policy=LLMPolicy(max_calls=2, concurrency=1))
    report, _, _ = run_benchmark(bench(), random_pairs=60_000, adjudicator=capped)
    stats = report.run["adjudication"]
    assert stats["calls"] == 2 and stats["unanswered"]["skipped"] > 0


def test_related_answers_become_relationship_candidates():
    report, engine, _ = run_benchmark(bench(), random_pairs=60_000, adjudicator=oracle())
    assert report.run["adjudication"]["decisions"].get("related", 0) > 0
    assert any("model_said_related" in c.signals for c in engine.relationship_candidates())


@pytest.mark.parametrize("local", [False, True])
def test_the_model_sees_only_what_the_profile_allows(local):
    report, engine, _ = run_benchmark(people.generate(300, seed=11), random_pairs=60_000,
                                      adjudicator=oracle(local=local))  # fmt: skip
    assert report.run["adjudication"]["answered"] > 0
    records = [side for i in engine.invocations() for p in i.payload["pairs"]
               for side in (p["a"], p["b"])]  # fmt: skip
    assert all("identifiers" not in r["fields"] for r in records)  # forbidden everywhere
    # An address is local-only: a local model sees it, a hosted one never does.
    assert any("address_line" in r["fields"] for r in records) is local
    assert any(r["fields"].get("date_of_birth") == "[redacted]" for r in records)
    assert any(str(r["fields"].get("email", [""])[0]).startswith("#") for r in records)
    assert any("identifiers" in r["withheld"] for r in records)
    assert check(engine.store) == []


def test_a_models_same_yields_to_a_contradiction():
    from thisness.engine import yield_to_conflicts

    # a-b-c joined by the model's "same" edges, with a negative edge a-c: they yield.
    # d-e, a model "same" in a component without a conflict, stands.
    edges = {("a", "b"): "strong", ("b", "c"): "strong", ("a", "c"): "negative",
             ("d", "e"): "strong", ("e", "f"): "strong"}  # fmt: skip
    yielded = yield_to_conflicts(edges, {("a", "b"), ("d", "e")})
    assert yielded == {("a", "b")}
    assert edges[("a", "b")] == "contested" and edges[("b", "c")] == "strong"
    assert edges[("d", "e")] == "strong"
    # Only the model's own "different" (or a cannot-link) contradicts its "same": a
    # negative edge from the evidence alone, such as an old address, does not.
    evidence = {("a", "b"): "strong", ("b", "c"): "strong", ("a", "c"): "negative"}
    assert yield_to_conflicts(evidence, {("a", "b")}, model_apart=set()) == set()
    assert evidence[("a", "b")] == "strong"
    model = {("a", "b"): "strong", ("b", "c"): "strong", ("a", "c"): "negative"}
    assert yield_to_conflicts(model, {("a", "b")}, model_apart={("a", "c")}) == {("a", "b")}
    hard = {("a", "b"): "strong", ("b", "c"): "strong", ("a", "c"): "cannot_link"}
    assert yield_to_conflicts(hard, {("a", "b")}, model_apart=set()) == {("a", "b")}


def test_yielding_does_not_cost_a_right_model_on_organizations(baseline):
    """The broad rule (any negative edge) discarded a right model's "same" across a
    company's own conflicting evidence: oracle recall fell from .983 to .964."""
    report, _, _ = run_benchmark(bench(), random_pairs=60_000, adjudicator=oracle("decide"))
    unyielding, _, _ = run_benchmark(
        bench(), random_pairs=60_000,
        adjudicator=lambda e, t, b: replace(oracle("decide")(e, t, b), same_yields=False),
    )  # fmt: skip
    assert report.clusters.b3_recall >= unyielding.clusters.b3_recall - 0.002


def test_yielding_keeps_a_wrong_same_from_holding_a_taxon():
    """With an authority's rows, a model wrong 10% of the time held whole taxa when its
    "same" stood against its own "different" answers (design 06 §10.7)."""
    from thisness.benchmark import taxa

    bench = taxa.from_fixture(200, reference=True)

    def wrong(yields):
        def build(engine, truth, b):
            provider = oracle_provider(benchmark_truth(engine, truth), related(b), error=0.1)
            client = LLMClient(engine.store, provider, ids=engine.ids, clock=engine.clock,
                               policy=LLMPolicy(concurrency=1))  # fmt: skip
            return Adjudicator(client, model="oracle", mode="decide", max_pairs=None,
                               same_yields=yields)  # fmt: skip

        return build

    held, _, _ = run_benchmark(bench, random_pairs=60_000, adjudicator=wrong(False))
    yielded, engine, _ = run_benchmark(bench, random_pairs=60_000, adjudicator=wrong(True))
    assert yielded.review_observations < held.review_observations
    assert yielded.clusters.b3_recall > held.clusters.b3_recall
    assert check(engine.store) == []
    assert Adjudicator(None, same_yields=False).ref.version == "1"  # the old policy's ref
