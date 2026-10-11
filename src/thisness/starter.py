"""Starter models: cold start for stores too small to fit their own (design 03 §21).

A starter is a Fellegi-Sunter model trained on the synthetic benchmark and shipped
as a package resource. The engine uses it until the store has enough observations of
the type to fit, then replaces it with a fitted model. Its weights reflect synthetic
data, so it is a prior, not a substitute for fitting.

A starter records the comparison components it was trained with; loading refuses a
starter whose comparisons no longer match the profile.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from thisness.fellegi_sunter import FSModel
from thisness.profiles.base import ResolutionProfile

FORMAT = 1
STARTER_VERSION = "starter.1"
STARTER_SEED = 20261008  # distinct from the evaluation seeds
STARTER_SIZE = {"organization": 4000, "person": 4000, "taxon": 2500}
DATA = "data"


class StarterMismatch(ValueError):
    """The shipped starter was trained with different comparisons than the profile has."""


def _comparisons(profile: ResolutionProfile) -> list[str]:
    return [c.ref.ref for c in profile.comparisons]


def load_starter(profile: ResolutionProfile) -> FSModel | None:
    if profile.starter_model is None:
        return None
    resource = resources.files("thisness.profiles") / DATA / profile.starter_model
    if not resource.is_file():
        return None
    payload = json.loads(resource.read_text())
    if payload.get("format") != FORMAT:
        raise StarterMismatch(
            f"{profile.starter_model}: unsupported format {payload.get('format')}"
        )
    if payload["comparisons"] != _comparisons(profile):
        raise StarterMismatch(
            f"{profile.starter_model} was trained with comparisons {payload['comparisons']}; "
            f"the profile now has {_comparisons(profile)}. Rebuild it with "
            "`python -m thisness.benchmark starter`."
        )
    return FSModel.from_config(payload["model"], version=payload["model_version"])


def build_starter(
    profile: ResolutionProfile,
    *,
    size: int | None = None,
    seed: int = STARTER_SEED,
    random_pairs: int = 300_000,
) -> dict[str, Any]:
    """Train on a fresh benchmark of the profile's type and return the payload to ship."""
    from thisness.benchmark import organizations, people, taxa
    from thisness.engine import Thisness
    from thisness.ids import IdFactory
    from thisness.training import train

    def taxon_starter(n: int, seed: int):
        # Families the evaluation benchmark never uses (design 06 §5).
        # With WFO's rows as observations: their assertion-blocked sessions are where
        # the name comparison can be learned at all (design 06 §10.2).
        return taxa.generate(
            taxa.families(taxa.STARTER_FAMILIES), n, seed, taxa.STARTER_FAMILIES, reference=True
        )

    generators = {"organization": organizations.generate, "person": people.generate,
                  "taxon": taxon_starter}  # fmt: skip
    size = size or STARTER_SIZE[profile.entity_type]
    benchmark = generators[profile.entity_type](size, seed=seed)
    now = datetime(2026, 10, 8, tzinfo=UTC)
    engine = Thisness(profiles=[profile], ids=IdFactory.seeded(seed), clock=lambda: now,
                      random_pairs=random_pairs)  # fmt: skip
    engine.ingest([
        engine.observe(profile.entity_type, r.attributes,
                       {"type": getattr(r, "source", "benchmark"), "id": r.record_id})
        for r in benchmark.records
    ])  # fmt: skip
    scope = engine.store.observation_ids(profile.entity_type)
    run = engine._new_run(profile, scope, purpose="starter")
    fitted = train(engine.store, profile, run.run_id, random_pairs=random_pairs, seed=0,
                   version=STARTER_VERSION)  # fmt: skip
    return {
        "format": FORMAT,
        "model_version": STARTER_VERSION,
        "comparisons": _comparisons(profile),
        "trained_on": benchmark.config,
        "random_pairs": random_pairs,
        "model": fitted.config(),
    }


def write_starter(profile: ResolutionProfile, path: Path, **kwargs: Any) -> Path:
    payload = build_starter(profile, **kwargs)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    return path
