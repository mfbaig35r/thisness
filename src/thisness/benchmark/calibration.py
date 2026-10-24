"""The calibration experiment (design 10 §4.3): do answered reviews close more reviews
than were answered?

A simulated reviewer (the oracle) answers the most impactful open reviews. Their answers
are labels on the pairs they covered (``thisness.labels``); the labels set new edge
thresholds; every other open review is triaged again with those thresholds, and a review
whose pairs all leave the contested band, without a contradiction inside a group, is
closed with the grouping its strong pairs give. Leverage is reviews closed by triage per
review answered. This module measures; it changes no engine behaviour.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from thisness.benchmark.evaluate import cluster_metrics
from thisness.benchmark.oracle import benchmark_truth
from thisness.core.decisions import Actor

ORACLE = Actor(kind="human", id="oracle")
TRIAGE = Actor(kind="system", id="calibration")


def wilson_upper(bad: int, n: int, z: float = 1.96) -> float:
    """The upper end of the Wilson interval for a rate of ``bad`` in ``n``."""
    if n == 0:
        return 1.0
    p = bad / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + spread) / (1 + z * z / n))


def calibrated_thresholds(labelled: Sequence[tuple[float, bool]], t_high: float, t_low: float,
                          epsilon: float) -> tuple[float, float]:  # fmt: skip
    """New thresholds from labelled pairs (weight, is_match): the lowest weight above which
    non-matches are rare, and the highest below which matches are rare, rare meaning the
    Wilson bound on the rate is at most ``epsilon``. A threshold the labels cannot
    support stays where it was; neither moves past the other."""
    weights = sorted({w for w, _ in labelled})
    high, low = t_high, t_low
    for w in weights:  # ascending: the first that holds is the lowest
        if w >= t_high:
            break
        above = [m for x, m in labelled if x >= w]
        if wilson_upper(sum(not m for m in above), len(above)) <= epsilon:
            high = w
            break
    for w in reversed(weights):  # descending: the first that holds is the highest
        if w <= t_low:
            break
        below = [m for x, m in labelled if x <= w]
        if wilson_upper(sum(below), len(below)) <= epsilon:
            low = w
            break
    if low >= high:
        return t_high, t_low
    return high, low


def _weight(scores: Mapping[str, Any]) -> float | None:
    if "fellegi_sunter_weight" not in scores:
        return None
    return float(scores["fellegi_sunter_weight"]) + float(scores.get("tf_adjustment", 0.0))


def _triage(members: Sequence[str], weights: Mapping[tuple[str, str], float], high: float,
            low: float) -> list[list[str]] | str:  # fmt: skip
    """The grouping a review's scored pairs give under new thresholds, or why there is
    none: "contested" when a pair is still in the band, "conflict" when a negative pair
    falls inside a group."""
    parent = {m: m for m in members}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    negatives = []
    for (a, b), w in weights.items():
        if w >= high:
            parent[find(a)] = find(b)
        elif w <= low:
            negatives.append((a, b))
        else:
            return "contested"
    if any(find(a) == find(b) for a, b in negatives):
        return "conflict"
    groups: dict[str, list[str]] = {}
    for m in members:
        groups.setdefault(find(m), []).append(m)
    return sorted(sorted(g) for g in groups.values())


@dataclass(frozen=True)
class CalibrationResult:
    answered: int
    labels: int
    thresholds_before: tuple[float, float]
    thresholds_after: tuple[float, float]
    triaged: int  # reviews closed by triage
    triage_right: int
    triage_merged_different: int
    triage_left_apart: int
    still_open: dict[str, int]  # why the rest stayed open
    contested_share: float  # of the open reviews' scored pairs, the share still contested
    reviews_before: int
    reviews_after: int
    after_answers: dict[str, float]  # precision, recall, false merges after the oracle only
    after_triage: dict[str, float]

    @property
    def leverage(self) -> float:
        return self.triaged / self.answered if self.answered else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "leverage": round(self.leverage, 3)}


def _metrics(engine, truth, kind) -> dict[str, float]:
    m = cluster_metrics(engine.store.entities_of(list(truth)), truth, kind)
    return {"b3_precision": round(m.b3_precision, 4), "b3_recall": round(m.b3_recall, 4),
            "false_merge_clusters": m.false_merge_clusters}  # fmt: skip


def run(engine, truth: Mapping[str, str], bench, *, answer: int, t_high: float, t_low: float,
        epsilon: float = 0.01) -> CalibrationResult:  # fmt: skip
    """The oracle answers the ``answer`` most impactful reviews; then calibrate and triage."""
    truth_of = benchmark_truth(engine, truth)
    reviews_before = len(engine.reviews())
    answered = 0
    for summary in engine.reviews()[:answer]:
        review = summary.review
        by_truth: dict[str | None, list[str]] = {}
        for node in review.members:
            by_truth.setdefault(truth_of(node), []).append(node)
        engine.resolve_review(review.review_id, "groups", groups=list(by_truth.values()),
                              reason="oracle", actor=ORACLE)  # fmt: skip
        answered += 1
    after_answers = _metrics(engine, truth, bench.kind)

    labelled = [(w, label.label == "match") for label in engine.labels()
                if (w := _weight(label.scores)) is not None]  # fmt: skip
    high, low = calibrated_thresholds(labelled, t_high, t_low, epsilon)

    triaged = right = merged = apart = 0
    still_open: dict[str, int] = {}
    pairs_open = contested = 0
    for summary in engine.reviews():
        review = summary.review
        members = list(review.members)
        latest: dict[tuple[str, str], Any] = {}
        for j in engine.store.pair_judgments(members):
            if j.actor.kind != "resolver":
                continue
            key = (j.left_id, j.right_id)
            if key not in latest or j.decision_id > latest[key].decision_id:
                latest[key] = j
        weights = {k: w for k, j in latest.items()
                   if (w := _weight({s.kind: s.value for s in j.scores})) is not None}  # fmt: skip
        groups = _triage(members, weights, high, low) if weights else "unscored"
        if isinstance(groups, str):
            still_open[groups] = still_open.get(groups, 0) + 1
            pairs_open += len(weights)
            contested += sum(low < w < high for w in weights.values())
            continue
        truths = [[truth_of(n) for n in g] for g in groups]
        mixed = any(len(set(g)) > 1 for g in truths)
        seen: dict[str | None, int] = {}
        for g in truths:
            for t in set(g):
                seen[t] = seen.get(t, 0) + 1
        split = any(c > 1 for t, c in seen.items() if t is not None)
        merged += mixed
        apart += split
        right += not (mixed or split)
        engine.resolve_review(review.review_id, "groups", groups=groups,
                              reason="closed by calibrated thresholds", actor=TRIAGE)  # fmt: skip
        triaged += 1
    return CalibrationResult(
        answered=answered, labels=len(labelled), thresholds_before=(t_high, t_low),
        thresholds_after=(round(high, 2), round(low, 2)), triaged=triaged, triage_right=right,
        triage_merged_different=merged, triage_left_apart=apart, still_open=still_open,
        contested_share=round(contested / pairs_open, 3) if pairs_open else 0.0,
        reviews_before=reviews_before, reviews_after=len(engine.reviews()),
        after_answers=after_answers, after_triage=_metrics(engine, truth, bench.kind),
    )  # fmt: skip
