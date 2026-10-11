from __future__ import annotations

import json

import pytest

from thisness.benchmark import organizations
from thisness.benchmark.evaluate import run_benchmark
from thisness.benchmark.oracle import benchmark_truth, oracle_provider
from thisness.core.run import GroupProposal, ProposalMember
from thisness.llm import LLMClient, LLMPolicy
from thisness.llm.providers import FunctionProvider
from thisness.name_only import NameOnlyPlan, classify, collapse_key, pair_stats, windows
from thisness.profiles.organization import organization
from thisness.storage.duckdb.invariants import check

# -- pieces ---------------------------------------------------------------------------------


def test_windows_overlap_and_reach_the_end():
    order = [f"n{i}" for i in range(10)]
    assert windows(order, 4, 0.5) == [order[0:4], order[2:6], order[4:8], order[6:10]]
    assert windows(order, 4, 0.0)[-1] == order[6:10]  # the last window reaches the end
    assert windows(order[:3], 4, 0.5) == [order[:3]]
    assert windows(order[:1], 4, 0.5) == []


def test_collapse_drops_account_numbers_and_keeps_legal_form():
    profile = organization()

    def key(name):
        from types import SimpleNamespace

        view: dict[str, list[str]] = {}
        for normalizer in profile.normalizers:
            for f in normalizer.normalize(SimpleNamespace(attributes={"name": name})):
                view.setdefault(f.feature, []).append(f.value)
        return collapse_key(view)

    assert key("Amazon - 812773") == key("AMAZON") == key("Amazon #019283")
    assert key("Amazon Inc") == key("Amazon, Inc. 4411") != key("Amazon")
    assert key("Premier Services GmbH") != key("Premier Services LLC")


def proposal(stage, batch, groups, shown=None):
    members = []
    for i, group in enumerate(groups):
        for node in group:
            text = (shown or {}).get(node, node)
            members.append(ProposalMember(node_id=node, shown=text,
                                          group_no=i if len(group) > 1 else None,
                                          label_out=f"L{i}"))  # fmt: skip
    return GroupProposal(proposal_id=f"prop_01J{batch:0>23}", run_id="run_01J" + "0" * 23,
                         stage_id=stage, batch_id=str(batch), members=tuple(members))  # fmt: skip


def test_one_vote_per_stage_and_echoes_do_not_count():
    a, b, c = "obs_01A" + "0" * 23, "obs_01B" + "0" * 23, "obs_01C" + "0" * 23
    raw, verify, chain = "stg_01R" + "0" * 23, "stg_01V" + "0" * 23, "stg_01D" + "0" * 23
    views = {raw: "raw", verify: "raw", chain: "derived"}
    proposals = [
        proposal(raw, 1, [[a, b], [c]]),
        proposal(raw, 2, [[a], [b], [c]]),  # an overlapping window disagreed
        proposal(verify, 3, [[a, b, c]]),
        proposal(chain, 4, [[a, c]], shown={a: "Acme", c: "Acme"}),  # identical label: echo
    ]
    stats = pair_stats(proposals, views)
    ab = stats[(a, b)]
    assert ab.raw == {raw: False, verify: True} and classify(ab, 2) == "contested"
    assert stats[(a, c)].echoes == 1 and stats[(a, c)].derived == {}
    assert classify(stats[(a, c)], 2) == "contested"  # raw: kept apart once, grouped once
    only_derived = pair_stats([proposal(chain, 5, [[a, b]])], views)[(a, b)]
    assert classify(only_derived, 2) == "unverified"
    assert classify(None, 2) == "none"


# -- runs ------------------------------------------------------------------------------------


def bench(n=150):
    return organizations.names_only(organizations.generate(n, seed=11))


def oracle_plan(policy=None, **errors):
    def build(engine, truth, b):
        def related(x, y):
            return b.group.get(x) is not None and b.group.get(x) == b.group.get(y)

        provider = oracle_provider(benchmark_truth(engine, truth), related, local=False, **errors)
        client = LLMClient(engine.store, provider, ids=engine.ids, clock=engine.clock,
                           policy=policy or LLMPolicy(concurrency=1))  # fmt: skip
        return NameOnlyPlan(client)

    return build


def test_name_only_resolves_what_names_alone_cannot():
    baseline, _, _ = run_benchmark(bench(), random_pairs=60_000)
    report, engine, _ = run_benchmark(bench(), random_pairs=60_000, plan=oracle_plan())
    c = report.clusters
    assert c.b3_recall > baseline.clusters.b3_recall + 0.3
    # 0.975: every false merge is distinct companies with identical names ("Premier
    # Services AG"), which no method can separate by name alone (design 05 §11.4).
    assert c.b3_precision >= 0.97
    assert report.run["plan"] == "name_only" and report.run["collapsed_groups"] > 0
    proposals = engine.store.proposals(report.run.get("run_id") or engine.runs()[0]["run_id"])
    assert proposals and all(p.invocation_id for p in proposals)
    votes = [d for d in engine.store.decisions_of_type("pair") if d.actor.id.startswith("votes:")]
    consensus = engine.store.decisions_of_type("consensus")
    cited = {e for d in consensus for e in d.flags.get("edges", ())}
    assert {v.decision_id for v in votes} & cited
    assert check(engine.store) == []


def test_later_records_join_existing_entities_by_name():
    _, engine, truth = run_benchmark(bench(), random_pairs=60_000, plan=oracle_plan())
    name, entity = next((o.attributes["name"], engine.entity_of(o.observation_id))
                        for o in engine.store.observations(sorted(truth)))  # fmt: skip
    new = engine.observe("organization", {"name": f"{name} - 998877"}, {"type": "ap", "id": "x"})
    plan = oracle_plan()(engine, {**truth, new.observation_id: truth[engine.members(entity)[0]]},
                         None)  # fmt: skip
    result = engine.resolve_many("organization", [new], plan=plan)
    assert engine.entity_of(new.observation_id) == entity
    assert result.stats["entities_matched"] >= 1
    assert check(engine.store) == []


def test_unusable_answers_and_no_budget_leave_names_provisional():
    def bad(engine, truth, b):
        def answer(request):
            items = json.loads(request.messages[0].content)["items"]
            refs = [i["ref"] for i in items]
            return {"groups": [{"members": refs[:2]}, {"members": refs[1:3]}]}  # overlapping

        client = LLMClient(engine.store, FunctionProvider(answer), ids=engine.ids,
                           clock=engine.clock, policy=LLMPolicy(concurrency=1))  # fmt: skip
        return NameOnlyPlan(client)

    for factory in (bad, oracle_plan(policy=LLMPolicy(max_calls=0, concurrency=1))):
        report, engine, truth = run_benchmark(bench(60), random_pairs=60_000, plan=factory)
        assert report.run["proposals"] == 0 and report.run["never_shown"] == report.run["nodes"]
        states = {
            engine.store.entity(e).state
            for e in set(engine.store.entities_of(list(truth)).values())
        }
        assert states == {"provisional"}
        assert check(engine.store) == []


@pytest.mark.parametrize("fold", [0.0, 0.3])
def test_a_model_that_folds_related_companies_costs_precision(fold):
    report, _, _ = run_benchmark(bench(), random_pairs=60_000, plan=oracle_plan(fold_related=fold))
    mixed = report.clusters.mixed_by_kind.get("family", 0)
    assert (mixed > 0) == (fold > 0)  # group companies merge only when the model folds them
