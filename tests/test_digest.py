from thisness.benchmark.digest import differences, store_digest
from thisness.benchmark.evaluate import run_benchmark
from thisness.benchmark.organizations import generate


def test_the_same_run_twice_gives_identical_stores(tmp_path):
    """Evaluation runs are deterministic, which is what design 10's identical-output
    checks rest on."""
    a, b = tmp_path / "a.duckdb", tmp_path / "b.duckdb"
    for path in (a, b):
        _, engine, _ = run_benchmark(generate(60, seed=3), random_pairs=20_000, store=str(path))
        engine.store.close()
    assert differences(a, b) == []
    digest = store_digest(a)
    assert digest["observations"][0] > 0 and digest["decisions"][0] > 0


def test_a_change_is_reported(tmp_path):
    a, b = tmp_path / "a.duckdb", tmp_path / "b.duckdb"
    for path, seed in ((a, 3), (b, 4)):
        _, engine, _ = run_benchmark(generate(60, seed=seed), random_pairs=20_000,
                                     store=str(path))  # fmt: skip
        engine.store.close()
    assert any(line.startswith("observations:") for line in differences(a, b))


def test_writing_in_chunks_changes_nothing(tmp_path):
    """Evidence and judgments are written a chunk at a time (design 10 §3.2): a chunk of
    seven pairs gives the same store, ids included, as one chunk for everything."""
    a, b = tmp_path / "small.duckdb", tmp_path / "whole.duckdb"
    for path, chunk in ((a, 7), (b, 10**9)):
        _, engine, _ = run_benchmark(generate(60, seed=3), random_pairs=20_000, store=str(path),
                                     engine_kwargs={"write_chunk": chunk})  # fmt: skip
        engine.store.close()
    assert differences(a, b) == []
