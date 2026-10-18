from __future__ import annotations

import math
import random
from collections import Counter

import pytest

from thisness.fellegi_sunter import (
    FSModel,
    Session,
    TermFrequency,
    em,
    fit,
    level_distribution,
)

LEVELS = {"name": ("exact", "close", "else"), "city": ("same", "else"), "id": ("same", "else")}
COMPARISONS = ("name", "city", "id")
TRUE_M = {"name": (0.7, 0.25, 0.05), "city": (0.9, 0.1), "id": (0.95, 0.05)}
TRUE_U = {"name": (0.01, 0.04, 0.95), "city": (0.1, 0.9), "id": (0.002, 0.998)}


def simulate(n: int, lam: float, seed: int, missing: float = 0.0) -> Counter:
    """Patterns from a known model with conditional independence."""
    rng = random.Random(seed)
    out: Counter = Counter()
    for _ in range(n):
        dist = TRUE_M if rng.random() < lam else TRUE_U
        pattern = []
        for c in COMPARISONS:
            if rng.random() < missing:
                pattern.append(None)
                continue
            pattern.append(rng.choices(range(len(LEVELS[c])), weights=dist[c])[0])
        out[tuple(pattern)] += 1
    return out


def test_em_recovers_known_parameters():
    patterns = simulate(40_000, lam=0.3, seed=1, missing=0.1)
    u = [TRUE_U[c] for c in COMPARISONS]
    m0 = [[0.9, 0.05, 0.05], [0.9, 0.1], [0.9, 0.1]]
    m, lam = em(patterns, u, [0, 1, 2], m0)
    assert lam == pytest.approx(0.3, abs=0.01)
    for i, c in enumerate(COMPARISONS):
        assert m[i] == pytest.approx(TRUE_M[c], abs=0.02)


def test_u_from_random_pairs_is_the_level_distribution():
    patterns = Counter({(0, 1, None): 3, (2, 1, 0): 1})
    assert level_distribution(patterns, 0, 3, smoothing=0) == (0.75, 0.0, 0.25)
    assert level_distribution(patterns, 2, 2, smoothing=0) == (1.0, 0.0)


def _fit(sessions, **kwargs) -> FSModel:
    return fit(
        entity_type="organization",
        comparisons=COMPARISONS,
        levels=LEVELS,
        random_patterns=simulate(100_000, lam=0.0, seed=9),
        sessions=sessions,
        **kwargs,
    )


def test_blocked_comparisons_are_learned_only_where_unblocked():
    patterns = simulate(20_000, lam=0.3, seed=2)
    model = _fit(
        [
            Session("by_name", frozenset({"name"}), patterns),
            Session("by_id", frozenset({"id"}), patterns),
        ]
    )
    # name is learned only in by_id, id only in by_name, city in both: none unlearned.
    assert model.unlearned == ()
    assert set(model.sessions) == {"by_name", "by_id"}
    for c in COMPARISONS:
        assert model.m[c] == pytest.approx(TRUE_M[c], abs=0.03)
    assert model.lam == pytest.approx(0.3, abs=0.02)


def test_a_comparison_every_session_blocks_is_reported_unlearned():
    model = _fit([Session("names", frozenset({"name"}), simulate(5_000, 0.3, seed=3))])
    assert model.unlearned == ("name",)


def test_m_floor_bounds_any_single_disagreement():
    # Matches here never disagree on id: EM learns m(id=else) ~ 0.
    patterns = Counter({(0, 0, 0): 3_000, (2, 1, 1): 7_000})
    unfloored = _fit([Session("s", frozenset(), patterns)], m_floor=0.0)
    floored = _fit([Session("s", frozenset(), patterns)], m_floor=1e-3)
    assert unfloored.level_weight("id", 1) < -15
    assert floored.m["id"][1] == pytest.approx(1e-3)
    assert sum(floored.m["id"]) == pytest.approx(1.0)
    assert floored.level_weight("id", 1) > -10.5


def _model(**kwargs) -> FSModel:
    return FSModel(
        entity_type="organization",
        comparisons=COMPARISONS,
        levels=LEVELS,
        m=TRUE_M,
        u=TRUE_U,
        lam=0.25,
        **kwargs,
    )


def test_caps_bound_learned_weights():
    capped = _model(caps={"id": {"same": 4.0}})
    assert _model().level_weight("id", 0) == pytest.approx(math.log2(0.95 / 0.002))
    assert capped.level_weight("id", 0) == 4.0
    assert capped.level_weight("id", 0, capped=False) > 4.0
    assert capped.weight((0, None, 0)) == pytest.approx(math.log2(0.7 / 0.01) + 4.0)


def test_term_frequency_adjustment():
    model = _model(tf=TermFrequency("name", "name.key", exact_level=0, fuzzy_levels=(1,)))
    freqs = {"acme": 0.0001, "premierservices": 0.05}
    rare = model.tf_adjustment((0, None, None), "acme", "acme", freqs, floor=1e-5)
    common = model.tf_adjustment((0, None, None), "premierservices", "premierservices", freqs, 1e-5)
    assert rare > 0 > common
    # Fuzzy levels are penalized for a common value, never boosted for a rare one.
    assert model.tf_adjustment((1, None, None), "acme", "acmee", freqs, 1e-5) == 0.0
    assert model.tf_adjustment((1, None, None), "premierservices", "premierservice", freqs,
                               1e-5) < 0  # fmt: skip
    assert model.tf_adjustment((2, None, None), "premierservices", "x", freqs, 1e-5) == 0.0
    assert _model().tf_adjustment((0, None, None), "acme", "acme", freqs, 1e-5) == 0.0


def test_posterior_and_threshold_are_inverses():
    model = _model()
    t = model.weight_for_posterior(0.999)
    assert model.posterior(t) == pytest.approx(0.999)
    assert model.posterior(t + 5) > 0.999 > model.posterior(t - 5)


def test_config_round_trip_reproduces_the_ref():
    model = _fit([Session("s", frozenset(), simulate(5_000, 0.3, seed=4))],
                 caps={"id": {"same": 4.0}}, tf=TermFrequency("name", "name.key", 0, (1,)))  # fmt: skip
    restored = FSModel.from_config(model.config(), version=model.version)
    assert restored == model
    assert restored.ref == model.ref
    assert model.ref.ref.startswith("model:fellegi_sunter.organization@1#")
    assert model.ref is model.ref  # computed once: resolution reads it per pair (design 10)


def test_an_adjustment_without_boost_only_discounts():
    """A postcode's baseline is set by formation agents holding thousands of companies:
    an ordinary head office looked rarer than average and earned a bonus that merged a
    group's siblings (design 07 §13.4). Without boost, rare earns nothing."""
    spec = TermFrequency("name", "name.key", exact_level=0, boost=False)
    model = _model(tf=(spec,))
    freqs = {"acme": 0.0001, "premierservices": 0.05}
    assert model.tf_adjustment((0, None, None), "acme", "acme", freqs, 1e-5, spec=spec) == 0.0
    assert model.tf_adjustment((0, None, None), "premierservices", "premierservices", freqs,
                               1e-5, spec=spec) < 0  # fmt: skip
    again = FSModel.from_config(model.config())
    assert again.tfs[0].boost is False and again.ref == model.ref
