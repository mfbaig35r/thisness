"""The rewrite-and-hash baseline (design 05 §11.5), measured with a model that only
consolidates what it sees in one chunk."""

from thisness import Thisness
from thisness.benchmark import rewrite, uk
from thisness.benchmark.organizations import generate, names_only
from thisness.cli import main
from thisness.llm import LLMClient, LLMPolicy
from thisness.llm.providers.function import FunctionProvider


def _run(bench, provider, **kwargs):
    engine = Thisness(":memory:")
    policy = kwargs.pop("policy", LLMPolicy(concurrency=4))
    client = LLMClient(engine.store, provider, ids=engine.ids, clock=engine.clock, policy=policy)
    return rewrite.run(bench, client, model="oracle", concurrency=4, **kwargs)


def test_key_keeps_letters_and_digits_only():
    assert rewrite.key("Acme, Inc. #4411") == rewrite.key("ACME INC 4411") == "ACMEINC4411"


def test_chunks_overlap_and_cover_everything():
    items = list(range(95))
    chunks = rewrite._chunks(items, 50, 10)
    assert chunks[0][-10:] == chunks[1][:10]
    assert sorted({i for c in chunks for i in c}) == items
    assert rewrite._chunks([], 50, 10) == []


def test_ordering_later_passes_by_key_hash_loses_recall():
    """Variants consolidate only when a chunk shows them together. Ordered by key hash,
    later passes scatter them; ordered by name, they sit side by side. Precision is
    the same either way (the measured runs: recall .762 against .976)."""
    bench = names_only(generate(120, seed=3))
    as_built = _run(bench, rewrite.oracle(bench), order="as_built").metrics
    by_name = _run(bench, rewrite.oracle(bench), order="sorted").metrics
    assert by_name.b3_recall > 0.98
    assert as_built.b3_recall < by_name.b3_recall - 0.1
    assert abs(as_built.b3_precision - by_name.b3_precision) < 0.01


def test_identical_names_of_different_companies_merge():
    """A key shared by two companies is one node before any model sees it: the
    false merges no method on names alone avoids."""
    bench = names_only(generate(120, seed=3))
    result = _run(bench, rewrite.oracle(bench), order="sorted")
    shared = {k for k, owners in _owners(bench).items() if len(owners) > 1}
    assert shared, "the benchmark has companies with identical names"
    assert result.metrics.false_merge_clusters >= 1


def _owners(bench):
    owners = {}
    for r in bench.records:
        owners.setdefault(rewrite.key(r.attributes["name"]), set()).add(bench.truth[r.record_id])
    return owners


def test_unanswered_chunks_keep_their_names():
    bench = names_only(generate(40, seed=3))
    result = _run(bench, FunctionProvider(lambda request: None))
    assert result.stats["failed"] == result.stats["calls"] > 0
    assert result.stats["final_groups"] == result.stats["keys"]  # nothing consolidated


def test_a_spent_budget_stops_calls_without_failing_the_run():
    bench = names_only(generate(40, seed=3))
    result = _run(bench, rewrite.oracle(bench), policy=LLMPolicy(max_calls=2, concurrency=1))
    assert result.stats["calls"] == 2
    assert result.stats["budget_stops"] > 0
    assert len(result.assignment) == len(bench.records)


def test_names_only_on_the_uk_benchmark():
    bench = names_only(uk.from_fixture(20))
    assert bench.config["variant"] == "names_only"
    assert all(set(r.attributes) == {"name"} for r in bench.records)
    assert any(r.attributes["name"][-1].isdigit() for r in bench.records)  # account numbers


def test_eval_runs_the_baseline(capsys):
    assert main(["eval", "--names-only", "--plan", "rewrite", "--rewrite-order", "sorted",
                 "--size", "40", "--seeds", "3"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "records: B3 P" in out and '"order": "sorted"' in out


def test_eval_rewrite_keeps_no_store(tmp_path, capsys):
    assert main(["eval", "--names-only", "--plan", "rewrite", "--size", "40",
                 "--save", str(tmp_path / "x.duckdb")]) == 1  # fmt: skip
