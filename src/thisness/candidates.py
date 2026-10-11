"""Generic candidate generators (design 03 §5).

Each wraps one set-oriented store query with its parameters and a versioned
component ref, and returns the uniform candidate table. Profiles compose them;
nothing here knows about organizations.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import pyarrow as pa
import pyarrow.compute as pc

from thisness.core.components import ComponentRef
from thisness.profiles.base import CandidateGenerator, CandidateSource

CANDIDATE_SCHEMA = pa.schema(
    [
        ("left_id", pa.string()),
        ("right_id", pa.string()),
        ("generator_ref", pa.string()),
        ("rank", pa.int32()),
        ("score_kind", pa.string()),
        ("score_value", pa.float64()),
    ]
)


def _candidates(pairs: pa.Table, ref: ComponentRef, score_kind: str) -> pa.Table:
    n = pairs.num_rows
    scores = (
        pairs.column("score_value").cast(pa.float64())
        if "score_value" in pairs.column_names
        else pa.array([1.0] * n, pa.float64())
    )
    return pa.table(
        {
            "left_id": pairs.column("left_id"),
            "right_id": pairs.column("right_id"),
            "generator_ref": pa.array([ref.ref] * n, pa.string()),
            "rank": pa.nulls(n, pa.int32()),
            "score_kind": pa.array([score_kind] * n, pa.string()),
            "score_value": scores,
        },
        schema=CANDIDATE_SCHEMA,
    )


@dataclass(frozen=True)
class ExactValue:
    """Pairs sharing a value of one feature (an identifier, a domain, a name key)."""

    name: str
    feature: str
    blocks_on: frozenset[str]
    max_block: int = 100
    ref: ComponentRef = field(init=False)

    def __post_init__(self) -> None:
        config = {"feature": self.feature, "max_block": self.max_block}
        object.__setattr__(
            self, "ref", ComponentRef.of("candidate_generator", self.name, "1", config)
        )

    def generate(self, source: CandidateSource, run_id: str) -> pa.Table:
        pairs = source.blocking_pairs(run_id, self.feature, self.max_block)
        return _candidates(pairs, self.ref, "shared_value")


@dataclass(frozen=True)
class RareValue:
    """Pairs sharing one of each side's rarest values (rare name tokens)."""

    name: str
    feature: str
    blocks_on: frozenset[str]
    per_observation: int = 2
    max_df: int = 60
    ref: ComponentRef = field(init=False)

    def __post_init__(self) -> None:
        config = {
            "feature": self.feature,
            "per_observation": self.per_observation,
            "max_df": self.max_df,
        }
        object.__setattr__(
            self, "ref", ComponentRef.of("candidate_generator", self.name, "1", config)
        )

    def generate(self, source: CandidateSource, run_id: str) -> pa.Table:
        pairs = source.rare_value_pairs(run_id, self.feature, self.per_observation, self.max_df)
        return _candidates(pairs, self.ref, "shared_value_df")


@dataclass(frozen=True)
class CrossFeature:
    """Pairs where one side's ``left_feature`` equals the other's ``right_feature``."""

    name: str
    left_feature: str
    right_feature: str
    blocks_on: frozenset[str]
    max_block: int = 50
    ref: ComponentRef = field(init=False)

    def __post_init__(self) -> None:
        config = {
            "left_feature": self.left_feature,
            "right_feature": self.right_feature,
            "max_block": self.max_block,
        }
        object.__setattr__(
            self, "ref", ComponentRef.of("candidate_generator", self.name, "1", config)
        )

    def generate(self, source: CandidateSource, run_id: str) -> pa.Table:
        pairs = source.cross_feature_pairs(
            run_id, self.left_feature, self.right_feature, self.max_block
        )
        return _candidates(pairs, self.ref, "cross_feature_match")


@dataclass(frozen=True)
class Overlap:
    """Pairs sharing at least ``min_share`` of the smaller side's values (trigrams)."""

    name: str
    feature: str
    blocks_on: frozenset[str]
    min_share: float = 0.6
    max_df: int = 150
    ref: ComponentRef = field(init=False)

    def __post_init__(self) -> None:
        config = {"feature": self.feature, "min_share": self.min_share, "max_df": self.max_df}
        object.__setattr__(
            self, "ref", ComponentRef.of("candidate_generator", self.name, "1", config)
        )

    def generate(self, source: CandidateSource, run_id: str) -> pa.Table:
        pairs = source.overlap_pairs(run_id, self.feature, self.min_share, self.max_df)
        return _candidates(pairs, self.ref, "overlap_share")


def generate_all(
    source: CandidateSource, generators: Iterable[CandidateGenerator], run_id: str
) -> pa.Table:
    """Every generator's candidates, one row per (pair, generator)."""
    tables = [g.generate(source, run_id) for g in generators]
    if not tables:
        return CANDIDATE_SCHEMA.empty_table()
    return pa.concat_tables(tables)


def distinct_pairs(candidates: pa.Table) -> list[tuple[str, str]]:
    """The union of candidate pairs, deduplicated and sorted."""
    if candidates.num_rows == 0:
        return []
    unique = (
        candidates.select(["left_id", "right_id"]).group_by(["left_id", "right_id"]).aggregate([])
    )
    order = pc.sort_indices(unique, sort_keys=[("left_id", "ascending"), ("right_id", "ascending")])
    unique = unique.take(order)
    return list(
        zip(
            unique.column("left_id").to_pylist(), unique.column("right_id").to_pylist(), strict=True
        )
    )
