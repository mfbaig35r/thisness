"""Training a Fellegi-Sunter model from a store (design 03 §8.2-8.4).

Training is offline: ``train`` produces a new model version, ``register_model``
records it as a component, and runs use a fixed version. A run never retrains,
so its result never depends on its own scope.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

import pyarrow as pa

from thisness.candidates import generate_all
from thisness.comparisons.base import FeatureView
from thisness.fellegi_sunter import FSModel, Pattern, Session, fit
from thisness.profiles.base import ResolutionProfile
from thisness.storage.duckdb import DuckDBStore

EMPTY: FeatureView = {}


def pattern(profile: ResolutionProfile, a: FeatureView, b: FeatureView) -> Pattern:
    return tuple(c.compare(a, b) for c in profile.comparisons)


def random_patterns(
    profile: ResolutionProfile,
    features: Mapping[str, FeatureView],
    *,
    n_pairs: int,
    seed: int,
) -> Counter[Pattern]:
    """Level patterns of uniformly random observation pairs: almost all non-matches,
    and unaffected by blocking. This is where u comes from."""
    ids = sorted(features)
    if len(ids) < 2:
        return Counter()
    rng = random.Random(seed)
    out: Counter[Pattern] = Counter()
    for _ in range(n_pairs):
        a, b = rng.sample(ids, 2)
        out[pattern(profile, features[a], features[b])] += 1
    return out


def blocking_sessions(
    profile: ResolutionProfile,
    candidates: pa.Table,
    features: Mapping[str, FeatureView],
) -> list[Session]:
    """One session per group of generators that block on the same attributes. In a
    session, comparisons over those attributes are frozen (design 03 §8.3)."""
    by_ref = {g.ref.ref: g for g in profile.candidate_generators}
    groups: dict[frozenset[str], set[tuple[str, str]]] = defaultdict(set)
    names: dict[frozenset[str], set[str]] = defaultdict(set)
    for left, right, ref in zip(
        candidates.column("left_id").to_pylist(),
        candidates.column("right_id").to_pylist(),
        candidates.column("generator_ref").to_pylist(),
        strict=True,
    ):
        generator = by_ref[ref]
        if generator.ref.name in profile.em_excluded_generators:
            continue
        groups[generator.blocks_on].add((left, right))
        names[generator.blocks_on].add(generator.ref.name)
    sessions = []
    for blocks_on in sorted(groups, key=lambda b: sorted(b)):
        blocked = frozenset(c.name for c in profile.comparisons if c.attributes & blocks_on)
        patterns: Counter[Pattern] = Counter(
            pattern(profile, features.get(a, EMPTY), features.get(b, EMPTY))
            for a, b in sorted(groups[blocks_on])
        )
        sessions.append(Session("+".join(sorted(names[blocks_on])), blocked, patterns))
    return sessions


def train(
    store: DuckDBStore,
    profile: ResolutionProfile,
    run_id: str,
    *,
    random_pairs: int = 300_000,
    seed: int = 0,
    m_floor: float = 1e-3,
    version: str = "1",
    prior: FSModel | None = None,
) -> FSModel:
    """Fit on the store: u from random pairs over every observation of the profile's
    type, m and the match rate from the candidates of ``run_id``'s scope. A comparison
    no blocking session can learn takes its m from ``prior`` when given."""
    observation_ids = store.observation_ids(profile.entity_type)
    features = store.features(observation_ids)
    candidates = generate_all(store, profile.candidate_generators, run_id)
    return fit(
        entity_type=profile.entity_type,
        comparisons=[c.name for c in profile.comparisons],
        levels={c.name: c.levels for c in profile.comparisons},
        random_patterns=random_patterns(profile, features, n_pairs=random_pairs, seed=seed),
        sessions=blocking_sessions(profile, candidates, features),
        caps=profile.weight_caps,
        tf=profile.term_frequency,
        m_floor=m_floor,
        version=version,
        prior=prior,
    )


def register_model(store: DuckDBStore, model: FSModel) -> str:
    store.register_component(model.ref, model.config())
    return model.ref.ref


def load_model(store: DuckDBStore, ref: str) -> FSModel:
    config = store.component_config(ref)
    if config is None:
        raise KeyError(f"no registered model {ref}")
    version = ref.split("@", 1)[1].split("#", 1)[0]
    model = FSModel.from_config(config, version=version)
    if model.ref.ref != ref:
        raise ValueError(f"model config does not reproduce {ref}")
    return model


def term_frequencies(
    store: DuckDBStore, feature: str, *, resolved_only: bool = True, by: str = "entity"
) -> tuple[dict[str, float], float]:
    """Frequency of each value of ``feature`` among resolved entities (design 03 §8.5),
    or among all observations with ``by="observation"``, and the floor for unseen
    values."""
    table = store.feature_frequencies(feature, by=by, resolved_only=resolved_only)
    counts = dict(
        zip(table.column("value").to_pylist(), table.column("n").to_pylist(), strict=True)
    )
    total = sum(counts.values()) or 1
    return {v: n / total for v, n in counts.items()}, 1 / total


def frequencies_from_clusters(
    clusters: Sequence[Sequence[str]], features: Mapping[str, FeatureView], feature: str
) -> tuple[dict[str, float], float]:
    """The two-pass variant for an initial load: frequency over first-pass clusters."""
    counts: Counter[str] = Counter()
    for members in clusters:
        for value in {v for m in members for v in features.get(m, EMPTY).get(feature, ())}:
            counts[value] += 1
    total = sum(counts.values()) or 1
    return {v: n / total for v, n in counts.items()}, 1 / total
