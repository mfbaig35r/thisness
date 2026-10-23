from __future__ import annotations

import pytest

from thisness.candidates import distinct_pairs, generate_all
from thisness.core import Observation, ResolutionRun, RunStatus
from thisness.profiles.organization import organization
from thisness.scoring import evidence_items, score_pairs
from thisness.training import (
    frequencies_from_clusters,
    load_model,
    register_model,
    term_frequencies,
    train,
)

from .synthetic import observations, organizations


@pytest.fixture
def loaded(store, ids, now):
    """The small synthetic dataset, ingested, featurized and scoped in one run."""
    profile = organization()
    store.register_component(profile.ref, {})
    orgs = organizations()
    truth, obs = {}, []
    for i, (org_id, attributes) in enumerate(observations(orgs)):
        o = Observation.new(ids=ids, entity_type="organization", attributes=attributes,
                            source={"type": "synthetic", "id": "t", "locator": {"i": i}},
                            ingested_at=now)  # fmt: skip
        obs.append(o)
        truth[o.observation_id] = org_id
    store.ingest(obs)
    store.write_features(profile.features(obs))
    run = ResolutionRun(run_id=ids.run(), entity_type="organization",
                        profile_ref=profile.ref.ref, base_seq=store.head_seq(),
                        status=RunStatus.EXECUTING, plan={}, created_at=now)  # fmt: skip
    store.record_run(run, [o.observation_id for o in obs])
    return profile, run.run_id, truth, {o.org_id: o.kind for o in orgs}


@pytest.fixture
def model(store, loaded):
    profile, run_id, _, _ = loaded
    return train(store, profile, run_id, random_pairs=60_000, seed=1)


def test_training_learns_sensible_weights(model):
    assert 0.0 < model.lam < 1.0
    assert model.unlearned == ()
    name = model.comparisons.index("name")
    assert model.level_weight("name", 0) > 5  # exact name key supports identity
    assert model.level_weight("name", model.levels["name"].index("else")) < -5
    assert model.level_weight("name", model.levels["name"].index("brand_only")) == 0.0  # capped
    assert model.level_weight("domain", 0) == 4.0  # capped
    assert name == 0
    assert all(min(row) >= 1e-3 - 1e-12 for row in model.m.values())  # floored


def test_training_is_deterministic(store, loaded, model):
    profile, run_id, _, _ = loaded
    again = train(store, profile, run_id, random_pairs=60_000, seed=1)
    assert again.ref == model.ref


def test_models_register_and_reload(store, model):
    ref = register_model(store, model)
    assert load_model(store, ref) == model
    with pytest.raises(KeyError):
        load_model(store, ref.replace(ref[-12:], "0" * 12))


def test_scores_separate_matches_from_non_matches(store, loaded, model):
    profile, run_id, truth, _ = loaded
    pairs = distinct_pairs(generate_all(store, profile.candidate_generators, run_id))
    features = store.features(sorted(truth))
    scored = score_pairs(profile, model, features, pairs).to_pylist()
    true_w = sorted(r["weight"] for r in scored if truth[r["left_id"]] == truth[r["right_id"]])
    false_w = sorted(r["weight"] for r in scored if truth[r["left_id"]] != truth[r["right_id"]])
    assert true_w[len(true_w) // 2] - false_w[len(false_w) // 2] > 20  # medians, in bits
    # The label-free threshold is precision-first. Pair recall at it is deliberately
    # partial: consensus joins the rest through transitivity (design 01).
    t_high = model.weight_for_posterior(0.999)
    strong = [r for r in scored if r["weight"] >= t_high and not r["cannot_link"]]
    precision = sum(truth[r["left_id"]] == truth[r["right_id"]] for r in strong) / len(strong)
    assert precision > 0.97
    assert len(strong) > 0.3 * len(true_w)
    # The identifier rule and the registration conflict fire where they should.
    for r in scored:
        if r["rule_verdict"] == "MATCH":
            assert r["level_reg_id"] == "exact"
        if r["cannot_link"]:
            assert r["level_reg_id"] == "else"


def test_common_names_are_penalized(store, loaded, model):
    profile, _, truth, kind = loaded
    premier = sorted(o for o, org in truth.items() if kind[org] == "common")
    pairs = [(a, b) for i, a in enumerate(premier) for b in premier[i + 1 :]]
    features = store.features(premier)
    freqs, floor = term_frequencies(store, "name.key")
    plain = score_pairs(profile, model, features, pairs).column("weight").to_pylist()
    adjusted = score_pairs(profile, model, features, pairs, frequencies=freqs, floor=floor)
    assert all(a <= p for a, p in zip(adjusted.column("weight").to_pylist(), plain, strict=True))
    assert min(adjusted.column("tf_adjustment").to_pylist()) < -2


def test_frequencies_from_clusters_count_entities_not_records():
    features = {"a": {"name.key": ["acme"]}, "b": {"name.key": ["acme"]},
                "c": {"name.key": ["zeta"]}}  # fmt: skip
    one_entity, _ = frequencies_from_clusters([["a", "b"], ["c"]], features, "name.key")
    two_entities, _ = frequencies_from_clusters([["a"], ["b"], ["c"]], features, "name.key")
    assert one_entity["acme"] == 0.5 and two_entities["acme"] == pytest.approx(2 / 3)


def test_relationship_signal_marks_brand_only_pairs(store, ids, now, model):
    profile = organization()
    obs = [Observation.new(ids=ids, entity_type="organization", attributes={"name": n},
                           source={"type": "t", "id": "r", "locator": {"n": n}}, ingested_at=now)
           for n in ("Libid Holdings", "Libid Solutions")]  # fmt: skip
    store.ingest(obs)
    store.write_features(profile.features(obs))
    features = store.features([o.observation_id for o in obs])
    row = score_pairs(profile, model, features, [tuple(sorted(features))]).to_pylist()[0]
    # A shared brand with another business: design 07 §13.2 names it qualifier_differs.
    assert row["level_name"] == "qualifier_differs" and row["relationship_signal"]


def test_evidence_items_follow_log_policy_and_record(store, ids, now, loaded, model):
    profile, run_id, truth, _ = loaded
    pairs = distinct_pairs(generate_all(store, profile.candidate_generators, run_id))
    features = store.features(sorted(truth))
    scored = score_pairs(profile, model, features, pairs)
    items = evidence_items(profile, model, scored, features, new_id=ids.evidence,
                           created_at=now, run_id=run_id)  # fmt: skip
    rows = scored.to_pylist()
    non_null = sum(1 for r in rows for c in profile.comparisons if r[f"level_{c.name}"] is not None)
    fired = sum(r["cannot_link"] for r in rows) + sum(r["rule"] is not None for r in rows)
    assert len(items) == non_null + fired
    domain = next(i for i in items if i.kind == "compare.domain")
    # Domain reads the email attribute, whose log policy is "hash": inputs are hashed.
    assert all(len(v) == 16 for vs in domain.inputs.values() for v in vs)
    name = next(i for i in items if i.kind == "compare.name")
    assert any("." not in v for vs in name.inputs.values() for v in vs)  # names shown as-is
    store.register_component(model.ref, model.config())
    for c in profile.comparisons:
        store.register_component(c.ref, {})
    for c in (*profile.constraints, *profile.rules):
        store.register_component(c.ref, {})
    store.record_evidence(items[:500])
    assert store.evidence([items[0].evidence_id]) == [items[0]]


def test_scored_rows_read_like_dicts():
    import pyarrow as pa

    from thisness.scoring import ScoredRows

    rows = ScoredRows(pa.table({"left_id": ["a", "b"], "weight": [1.5, -2.0]}))
    assert len(rows) == 2 and rows[1]["weight"] == -2.0 and rows[-1]["left_id"] == "b"
    assert dict(rows[0]) == {"left_id": "a", "weight": 1.5}
    assert rows[0].get("missing") is None and [r["left_id"] for r in rows] == ["a", "b"]


def test_scored_rows_reuse_supplied_columns():
    import pyarrow as pa
    import pytest

    from thisness.scoring import ScoredRows

    ids = ["a", "b"]
    rows = ScoredRows(pa.table({"left_id": ["a", "b"], "weight": [1.0, 2.0]}),
                      columns={"left_id": ids})  # fmt: skip
    assert rows.column("left_id") is ids and rows[1]["left_id"] == "b"
    with pytest.raises(ValueError):
        ScoredRows(pa.table({"left_id": ["a"]}), columns={"left_id": ["a", "b"]})
