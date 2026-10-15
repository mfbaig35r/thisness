"""A store built by an earlier release keeps working: stale features are recomputed and a
model fitted for other comparisons is replaced (found cutting 0.2.0, whose organization
name normalizer and comparison changed)."""

from dataclasses import replace

from thisness import Thisness
from thisness.benchmark.evaluate import run_benchmark
from thisness.benchmark.organizations import generate
from thisness.engine import _fits
from thisness.profiles.organization import organization
from thisness.storage.duckdb.invariants import check


def test_features_from_another_normalizer_version_are_recomputed(tmp_path):
    path = str(tmp_path / "s.duckdb")
    engine = Thisness(path, profiles=[organization()])
    engine.ingest(
        [
            engine.observe(
                "organization", {"name": "SQ *ACME CORP - 812773"}, {"type": "t", "id": "1"}
            )
        ]
    )
    current = engine.store.feature_normalizers("organization")
    # As an earlier release would have written them.
    engine.store._con.execute("UPDATE observation_features SET normalizer_ref = 'normalizer:organization_name@2#old', "
                              "value = 'sqacmecorp812773' WHERE feature = 'name.key'")  # fmt: skip
    engine.store.close()
    reopened = Thisness(path, profiles=[organization()])
    assert reopened.store.feature_normalizers("organization") == current
    obs = reopened.store.observation_ids("organization")
    assert reopened.store.features(obs)[obs[0]]["name.key"] == ["acme"]
    assert check(reopened.store) == []


def test_a_model_fitted_for_other_levels_is_replaced():
    _, engine, _ = run_benchmark(generate(120, seed=3), random_pairs=20_000)
    profile = engine.profiles["organization"]
    model = engine.model("organization")
    assert _fits(model, profile)
    old_levels = {**model.levels, "name": (*model.levels["name"][:-3], "else")}  # 0.1.0's nine
    assert not _fits(replace(model, levels=old_levels), profile)
    assert not _fits(replace(model, comparisons=model.comparisons[:-1]), profile)
