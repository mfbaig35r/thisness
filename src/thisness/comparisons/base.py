"""Comparisons, constraints and rules (design 03 §6-7).

A comparison maps a pair of observations to exactly one level from an ordered
list, or ``None`` when either side lacks the attribute. Comparisons read the
normalized features already in the store, never raw attributes, so they agree
with blocking by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from thisness.core.components import ComponentRef

# observation id -> feature -> values in ordinal order
FeatureView = Mapping[str, list[str]]
Polarity = Literal["supports", "contradicts", "neutral"]


class Comparison(Protocol):
    name: str
    levels: tuple[str, ...]
    attributes: frozenset[str]  # source attributes; matched against generators' blocks_on
    features: tuple[str, ...]  # features it reads; shown as evidence inputs
    polarity: Mapping[str, Polarity]
    ref: ComponentRef

    def compare(self, a: FeatureView, b: FeatureView) -> int | None: ...


class Constraint(Protocol):
    """A hard rule over a pair. ``cannot_link`` returns a reason, or None."""

    name: str
    ref: ComponentRef

    def cannot_link(self, a: FeatureView, b: FeatureView) -> str | None: ...


class Rule(Protocol):
    """A deterministic rule over comparison levels. Returns a verdict name or None.

    Rules are decisive: when one fires, Fellegi-Sunter is still computed and stored,
    but does not change the verdict (design 03 §7)."""

    name: str
    ref: ComponentRef

    def apply(self, levels: Mapping[str, str | None]) -> str | None: ...


def single(view: FeatureView, feature: str) -> str | None:
    values = view.get(feature)
    return values[0] if values else None


def values(view: FeatureView, feature: str) -> set[str]:
    return set(view.get(feature, ()))
