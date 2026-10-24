"""Resolution profiles: what identity means for one entity type (design 02 §5).

A profile is configuration plus pluggable components. M1 step 3 defines
attributes, normalizers and candidate generators; comparisons, the resolver,
edge policy, consensus and policy join in later steps.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple, Protocol

import pyarrow as pa

from thisness.comparisons.base import Comparison, Constraint, Rule
from thisness.core.base import Model
from thisness.core.components import ComponentRef
from thisness.core.observation import Observation
from thisness.core.types import EntityType, Name
from thisness.fellegi_sunter import TermFrequency
from thisness.ids import validate_entity_prefix


class AttributeDefinition(Model):
    name: Name
    data_type: Literal["string", "integer", "number", "boolean", "date", "datetime", "json"]
    cardinality: Literal["single", "multi"] = "single"
    nullable: bool = True
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    log_policy: Literal["allow", "hash", "redact"] = "allow"
    external_model_policy: Literal["allow", "redact", "hash", "local_only", "forbidden"] = "allow"
    identity_strength: Literal["weak", "moderate", "strong"] | None = None  # documentation only
    temporal: bool = False
    display: str | None = None


@dataclass(frozen=True)
class AssertionFields:
    """The attributes in which a source says where a record belongs (design 06 §3): its
    status for the record's name, and the accepted name it gives when that is not the
    name itself. The dossier groups members by source and by that accepted name, so
    which authority said what is visible, and where one disagrees with the label."""

    status: str
    accepted: str
    name: str = "name"
    authorship: str | None = None  # a separate attribute completing the name, if any
    accepted_statuses: frozenset[str] = frozenset({"accepted"})
    reference: str | None = None  # the source whose word defines the entity


def _tf_list(tf: TermFrequency) -> list[Any]:
    extra = ([tf.count] if tf.count != "entity" or not tf.boost else []) + \
        (["no_boost"] if not tf.boost else [])  # fmt: skip
    return [tf.comparison, tf.feature, tf.exact_level, list(tf.fuzzy_levels), *extra]


def _assertion_config(spec: AssertionFields | None) -> dict[str, Any] | None:
    if spec is None:
        return None
    return {**vars(spec), "accepted_statuses": sorted(spec.accepted_statuses)}


@dataclass(frozen=True)
class FeatureConflict:
    """Members of one entity disagreeing on a normalized feature (design 04 §3).

    Reported under ``attribute``. With ``scheme_separator``, values are compared only
    within a scheme (the prefix before the separator, as in ``lei:5299...``): two LEIs
    contradict, an LEI and a DUNS number do not."""

    feature: str
    attribute: str
    scheme_separator: str | None = None

    def scheme(self, value: str) -> str | None:
        if self.scheme_separator is None or self.scheme_separator not in value:
            return None
        return value.split(self.scheme_separator, 1)[0]


class RelationshipPattern(NamedTuple):
    """Comparison levels that, all on one pair, suggest two related entities rather than
    one: a household is the same family name at the same address (design 04 §6.4)."""

    name: str
    levels: tuple[tuple[str, str], ...]


class Feature(NamedTuple):
    feature: str
    ordinal: int
    value: str | None
    value_num: float | None = None


class Normalizer(Protocol):
    """A pure, versioned function from an observation to feature rows."""

    ref: ComponentRef
    features: frozenset[str]

    def normalize(self, observation: Observation) -> list[Feature]: ...


class CandidateSource(Protocol):
    """The store operations candidate generators are built from (design 02 §8)."""

    def blocking_pairs(self, run_id: str, feature: str, max_block: int) -> pa.Table: ...
    def rare_value_pairs(
        self, run_id: str, feature: str, per_observation: int, max_df: int
    ) -> pa.Table: ...
    def cross_feature_pairs(
        self, run_id: str, left_feature: str, right_feature: str, max_block: int
    ) -> pa.Table: ...
    def overlap_pairs(
        self, run_id: str, feature: str, min_share: float, max_df: int
    ) -> pa.Table: ...


class CandidateGenerator(Protocol):
    """Recall-oriented retrieval. Returns ``left_id, right_id, generator_ref, rank,
    score_kind, score_value``; at least one side of every pair is in the run's scope."""

    ref: ComponentRef
    blocks_on: frozenset[str]  # attributes it blocks on; drives EM sessions (design 03 §8.3)

    def generate(self, source: CandidateSource, run_id: str) -> pa.Table: ...


FEATURE_SCHEMA = pa.schema(
    [
        ("observation_id", pa.string()),
        ("feature", pa.string()),
        ("ordinal", pa.int32()),
        ("value", pa.string()),
        ("value_num", pa.float64()),
        ("normalizer_ref", pa.string()),
    ]
)


@dataclass(frozen=True)
class ResolutionProfile:
    entity_type: EntityType
    id_prefix: str
    version: str
    attributes: dict[str, AttributeDefinition]
    normalizers: tuple[Normalizer, ...] = ()
    candidate_generators: tuple[CandidateGenerator, ...] = ()
    comparisons: tuple[Comparison, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    rules: tuple[Rule, ...] = ()
    # Profile-declared bounds on learned weights: comparison -> level -> max bits
    # (design 03 §8.6).
    weight_caps: dict[str, dict[str, float]] = field(default_factory=dict)
    # (comparison, level) pairs that signal a relationship, not identity.
    relationship_levels: frozenset[tuple[str, str]] = frozenset()
    # Conjunctions of levels that signal a relationship (all must hold on the pair).
    relationship_patterns: tuple[RelationshipPattern, ...] = ()
    # One common-value adjustment, or several on different comparisons.
    term_frequency: TermFrequency | tuple[TermFrequency, ...] | None = None
    # Features on which an entity's members should agree; disagreements are reported as
    # contradictions in its dossier (design 04 §3).
    contradictions: tuple[FeatureConflict, ...] = ()
    # Builds this type's snapshots; None for the engine's default (design 06 §6).
    canonicalizer: Any = None
    # Attributes carrying sources' assertions of where a record belongs (design 06 §3).
    assertions: AssertionFields | None = None
    # Domain guidance added to every model prompt for this type (design 06 §6); part of
    # the prompts' refs, so changing it is a new prompt version.
    prompt_hints: str | None = None
    # What a model is trusted to do for this type when one is configured (design 06
    # §10.11): "decide" contested pairs (the adjudicator in decide mode, its "same"
    # yielding to its own "different"), or "suggest" answers to reviews for a person to
    # accept. None leaves the adjudicator in shadow mode.
    model_role: Literal["decide", "suggest"] | None = None
    # The kinds of suggestion a person may accept in bulk for this type (design 10
    # §4.2), measured per domain like ``model_role``. It shapes a command a person runs,
    # not resolution, so it is not part of the profile's identity.
    accept_suggestions: tuple[str, ...] = ()
    # The name-only plan's collapse key (design 05 §6): records whose names reduce to
    # one key are one node. None for the organization key. A plain function of a
    # record's features; its name is part of the profile's identity.
    collapse_key: Callable[[Mapping[str, list[str]]], str | None] | None = None
    # Relationship names a reviewer is offered for this type (any name is allowed).
    relations: tuple[str, ...] = ("related_to",)
    # Attribute -> the feature that groups its values in a dossier.
    value_keys: dict[str, str] = field(default_factory=dict)
    # Floor on the automatic strong-edge threshold, in bits (design 03 §21). The
    # label-free threshold falls as the match rate among candidates rises, which tracks
    # how cleanly blocking prunes, not how hard the data is.
    min_strong_weight: float | None = None
    # Weight at or below which a pair is evidence against identity (a negative edge),
    # in place of the edge policy's T_low. For taxa, two names in one genus that share
    # nothing else weigh about zero: no evidence, not evidence against (design 06 §10.5).
    max_negative_weight: float | None = None
    # Package resource holding a model for stores too small to fit their own.
    starter_model: str | None = None
    persist_candidates: bool = False
    # Generators whose blocks are not used to learn m (design 04 §10): blocks on an
    # attribute that related entities share (a household's address and landline) hold
    # two kinds of pair, and EM learns "same household" as if it were identity.
    em_excluded_generators: frozenset[str] = frozenset()
    _ref: ComponentRef = field(init=False, repr=False, compare=False)

    @property
    def term_frequencies(self) -> tuple[TermFrequency, ...]:
        tf = self.term_frequency
        if tf is None:
            return ()
        return (tf,) if isinstance(tf, TermFrequency) else tuple(tf)

    def __post_init__(self) -> None:
        validate_entity_prefix(self.id_prefix)
        config = {
            "entity_type": self.entity_type,
            "id_prefix": self.id_prefix,
            "attributes": {k: v.model_dump(mode="json") for k, v in self.attributes.items()},
            "normalizers": [n.ref.ref for n in self.normalizers],
            "candidate_generators": [g.ref.ref for g in self.candidate_generators],
            "comparisons": [c.ref.ref for c in self.comparisons],
            "constraints": [c.ref.ref for c in self.constraints],
            "rules": [r.ref.ref for r in self.rules],
            "weight_caps": self.weight_caps,
            "relationship_levels": sorted(list(x) for x in self.relationship_levels),
            "relationship_patterns": [
                [p.name, [list(x) for x in p.levels]] for p in self.relationship_patterns
            ],
            "term_frequency": None
            if self.term_frequency is None
            else _tf_list(self.term_frequency)
            if isinstance(self.term_frequency, TermFrequency)
            else [_tf_list(t) for t in self.term_frequency],
            "contradictions": [
                [c.feature, c.attribute, c.scheme_separator] for c in self.contradictions
            ],
            "value_keys": self.value_keys,
            "relations": list(self.relations),
            "canonicalizer": self.canonicalizer.ref.ref if self.canonicalizer else None,
            "assertions": _assertion_config(self.assertions),
            "prompt_hints": self.prompt_hints,
            "model_role": self.model_role,
            "max_negative_weight": self.max_negative_weight,
            "collapse_key": (
                f"{self.collapse_key.__module__}.{self.collapse_key.__qualname__}"
                if self.collapse_key
                else None
            ),
            "min_strong_weight": self.min_strong_weight,
            "starter_model": self.starter_model,
            "persist_candidates": self.persist_candidates,
            "em_excluded_generators": sorted(self.em_excluded_generators),
        }
        object.__setattr__(
            self, "_ref", ComponentRef.of("profile", self.entity_type, self.version, config)
        )

    @property
    def ref(self) -> ComponentRef:
        return self._ref

    def relationship_signals(self, levels: Mapping[str, str | None]) -> list[str]:
        """The relationship signals a pair's comparison levels carry: ``comparison/level``
        for each relationship level, and the name of each pattern that holds."""
        held = {(c, label) for c, label in levels.items() if label is not None}
        out = [f"{c}/{label}" for c, label in sorted(held & self.relationship_levels)]
        out += [p.name for p in self.relationship_patterns if set(p.levels) <= held]
        return out

    @property
    def relationship_level_pairs(self) -> frozenset[tuple[str, str]]:
        """Every (comparison, level) any relationship signal reads."""
        return self.relationship_levels | {x for p in self.relationship_patterns for x in p.levels}

    def comparison(self, name: str) -> Comparison:
        for c in self.comparisons:
            if c.name == name:
                return c
        raise KeyError(name)

    def log_policy(self, comparison: Comparison) -> str:
        """The strictest log policy among the attributes a comparison reads."""
        order = {"allow": 0, "hash": 1, "redact": 2}
        policies = [
            self.attributes[a].log_policy for a in comparison.attributes if a in self.attributes
        ]
        return max(policies, key=order.__getitem__, default="allow")

    def features(self, observations: Sequence[Observation]) -> pa.Table:
        """Run every normalizer over the observations, as an Arrow table for the store."""
        rows = []
        for obs in observations:
            if obs.entity_type != self.entity_type or obs.attributes is None:
                continue
            for normalizer in self.normalizers:
                ref = normalizer.ref.ref
                for f in normalizer.normalize(obs):
                    rows.append(
                        {
                            "observation_id": obs.observation_id,
                            "feature": f.feature,
                            "ordinal": f.ordinal,
                            "value": f.value,
                            "value_num": f.value_num,
                            "normalizer_ref": ref,
                        }
                    )
        return pa.Table.from_pylist(rows, schema=FEATURE_SCHEMA)
