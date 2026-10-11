from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from thisness.assistant import ReviewAssistant
from thisness.benchmark import organizations, people
from thisness.benchmark.evaluate import run_benchmark
from thisness.benchmark.oracle import benchmark_truth, oracle_provider
from thisness.llm import LLMClient, LLMPolicy, ProviderError
from thisness.llm.providers import FunctionProvider
from thisness.storage.duckdb.invariants import check
from thisness.ui import create_app


def setup(bench, **errors):
    _, engine, truth = run_benchmark(bench, random_pairs=60_000)

    def related(a, b):
        return bench.group.get(a) is not None and bench.group.get(a) == bench.group.get(b)

    provider = oracle_provider(benchmark_truth(engine, truth), related, local=False, **errors)
    engine.assistant = ReviewAssistant(LLMClient(engine.store, provider, ids=engine.ids,
                                                 clock=engine.clock))  # fmt: skip
    return engine


def fake(engine, answer):
    """An assistant whose model answers with ``answer(payload)``."""
    provider = FunctionProvider(lambda r: answer(json.loads(r.messages[0].content)))
    return ReviewAssistant(LLMClient(engine.store, provider, ids=engine.ids, clock=engine.clock,
                                     policy=LLMPolicy(max_calls=5)))  # fmt: skip


@pytest.fixture
def engine():
    engine = setup(organizations.generate(300, seed=11))
    yield engine
    assert check(engine.store) == []


def test_a_recommendation_is_recorded_but_changes_nothing(engine):
    review = engine.reviews()[0].review
    result = engine.recommend(review.review_id)
    rec = result.recommendation
    assert result.status == "ok" and rec.actor.kind == "agent" and rec.invocation_id
    assert sorted(n for g in rec.groups for n in g) == sorted(review.members)
    assert engine.recommendations(review.review_id) == [rec]
    assert engine.review_case(review.review_id).review.status == "open"
    assert engine.labels() == []  # a suggestion is never a label
    sent = engine.invocation(rec.invocation_id).payload
    assert sent["question"].startswith("Are these") and sent["comparisons"]
    assert {r["ref"] for r in sent["records"]} == {f"R{i + 1}" for i in range(len(review.members))}


def test_agreement_is_measured_on_what_reviewers_decide(engine):
    results = engine.recommend_open(limit=6)
    assert len(results) == 6 and engine.recommend_open(limit=6) == []  # none asked twice
    agreed, overruled = results[0].recommendation, results[1].recommendation
    engine.resolve_review(agreed.review_id, "groups", groups=[list(g) for g in agreed.groups],
                          reason="as suggested", recommendation_id=agreed.decision_id)  # fmt: skip
    members = sorted(n for g in overruled.groups for n in g)
    split = [[m] for m in members] if len(overruled.groups) == 1 else [members]
    engine.resolve_review(overruled.review_id, "groups", groups=split, reason="no",
                          recommendation_id=overruled.decision_id)  # fmt: skip
    measured = engine.assistant_agreement()
    assert measured["reviews"] == 2 and measured["exact"] == 0.5
    assert 0 <= measured["pairwise"] < 1


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (lambda p: {"resolution": "groups", "groups": [["R1"]]}, "invalid_groups"),
        (lambda p: {"resolution": "same", "cited": ["C999"]}, "invalid_citation"),
        (lambda p: {"resolution": "related", "relation": "married_to"}, "unknown_relation"),
        (lambda p: {"resolution": "maybe"}, "failed"),
    ],
)
def test_unusable_answers_are_not_recorded(engine, answer, status):
    # A review over exactly two entities, so "related" is a possible answer.
    review_id = next(s.review.review_id for s in engine.reviews()
                     if len({n.entity_id for n in engine.review_case(s.review.review_id).nodes})
                     == 2)  # fmt: skip
    result = engine.recommend(review_id, assistant=fake(engine, answer))
    assert result.status == status and result.recommendation is None
    assert engine.recommendations(review_id) == []


def test_failures_and_limits(engine):
    def down(payload):
        raise ProviderError("overloaded")

    review_id = engine.reviews()[0].review.review_id
    assert engine.recommend(review_id, assistant=fake(engine, down)).status == "failed"
    capped = fake(engine, lambda p: {"resolution": "unsure"})
    statuses = [capped.recommend(engine, s.review.review_id).status for s in engine.reviews()[:7]]
    assert statuses == ["ok"] * 5 + ["skipped"] * 2


def test_a_hosted_model_sees_only_what_the_profile_allows():
    engine = setup(people.generate(300, seed=11))
    for summary in engine.reviews()[:5]:
        engine.recommend(summary.review.review_id)
    records = [r for c in engine.invocations() for r in c.payload["records"]]
    assert records and all("identifiers" not in r["fields"] and "address_line" not in r["fields"]
                           for r in records)  # fmt: skip
    assert check(engine.store) == []


def test_the_workbench_shows_and_records_suggestions(engine):
    client = TestClient(create_app(engine), follow_redirects=False)
    first, second = (s.review.review_id for s in engine.reviews()[:2])
    page = client.get(f"/reviews/{first}")
    assert "Ask the model" in page.text and "Suggestion" not in page.text
    asked = client.post(f"/reviews/{first}/recommend", headers={"HX-Request": "true"})
    assert asked.status_code == 204 and asked.headers["HX-Redirect"] == f"/reviews/{first}"
    page = client.get(f"/reviews/{first}")
    rec = engine.recommendations(first)[-1]
    assert "Suggestion" in page.text and "Apply to the board" in page.text
    assert rec.decision_id in page.text  # sent back with the answer
    r = client.post(f"/reviews/{first}/resolve", data={
        "resolution": "groups", "reason": "agreed", "groups": json.dumps([list(g) for g in rec.groups]),
        "recommendation_id": rec.decision_id})  # fmt: skip
    assert r.status_code == 303
    assert engine.assistant_agreement()["reviews"] == 1
    calls = client.get("/llm")
    assert calls.status_code == 200 and "recommend_review" in calls.text
    assert rec.invocation_id in client.get(f"/panel/decision/{rec.decision_id}").text
    panel = client.get(f"/panel/invocation/{rec.invocation_id}")
    assert "What was sent" in panel.text and "after privacy filtering" in panel.text
    engine.assistant = None
    assert "Ask the model" not in client.get(f"/reviews/{second}").text


def test_many_reviews_are_asked_in_parallel_and_recorded_in_order():
    """The model calls overlap; reading the store and recording answers do not, and the
    recommendations are the ones a one-at-a-time run records."""
    import threading
    import time

    def run(concurrency):
        engine = setup(organizations.generate(300, seed=11))
        oracle = engine.assistant.client.provider
        state = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def answer(request):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.02)
            try:
                return oracle.invoke(request)
            finally:
                with lock:
                    state["now"] -= 1

        provider = FunctionProvider(lambda r: None, name="oracle", local=False)
        provider.invoke = answer
        engine.assistant = ReviewAssistant(LLMClient(engine.store, provider, ids=engine.ids,
                                                     clock=engine.clock,
                                                     policy=LLMPolicy(concurrency=concurrency)))  # fmt: skip
        results = engine.recommend_open(limit=12)
        assert check(engine.store) == []
        return state["peak"], [(r.review_id, r.status, r.recommendation and r.recommendation.resolution)
                               for r in results]  # fmt: skip

    peak_one, one_at_a_time = run(1)
    peak_many, parallel = run(8)
    assert peak_one == 1 and peak_many > 1
    assert parallel == one_at_a_time and len(parallel) == 12


def test_the_workbench_shows_the_labels_the_model_saw(engine):
    """Cards and comparisons carry the R and C labels of the assistant's request, and
    the queue shows which reviews have a suggestion."""
    from thisness.assistant import review_labels

    review = engine.reviews()[0].review
    engine.recommend(review.review_id)
    client = TestClient(create_app(engine))
    page = client.get(f"/reviews/{review.review_id}").text
    refs, labelled = review_labels(engine, engine.review_case(review.review_id))
    assert all('class="ref"' in page and f">{r}<" in page for r in list(refs.values())[:3])
    if labelled:
        assert f">{labelled[0][0]}<" in page  # C1, the strongest comparison
    queue = client.get("/reviews").text
    assert "Suggestion" in queue and queue.count(">none<") == len(engine.reviews()) - 1
