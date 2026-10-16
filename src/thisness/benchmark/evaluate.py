"""Evaluation against ground truth (PRD §45, design 03 §11).

Cluster-level identity safety comes first: false-merge clusters, contaminated
observations, the largest erroneous cluster. Pair metrics are diagnostics only:
one bad edge contaminates a whole cluster while barely moving pair precision.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from typing import Any

from thisness.benchmark.organizations import Benchmark as OrganizationBenchmark
from thisness.benchmark.people import Benchmark as PeopleBenchmark
from thisness.benchmark.taxa import Benchmark as TaxonBenchmark
from thisness.consensus import components
from thisness.engine import RunResult, Thisness
from thisness.ids import IdFactory
from thisness.profiles.base import ResolutionProfile
from thisness.profiles.organization import organization
from thisness.profiles.person import person
from thisness.profiles.taxon import taxon

Benchmark = OrganizationBenchmark | PeopleBenchmark | TaxonBenchmark
PROFILES = {"organization": organization, "person": person, "taxon": taxon}


@dataclass(frozen=True)
class ClusterMetrics:
    observations: int
    clusters: int
    b3_precision: float
    b3_recall: float
    b3_f1: float
    false_merge_clusters: int
    contaminated: int  # observations not of their cluster's majority organization
    largest_bad_cluster: int
    by_kind_recall: dict[str, float] = field(default_factory=dict)
    mixed_by_kind: dict[str, int] = field(default_factory=dict)  # clusters mixing 2+ orgs of a kind


def cluster_metrics(
    assignment: Mapping[str, str], truth: Mapping[str, str], kind: Mapping[str, str]
) -> ClusterMetrics:
    """``assignment``: observation -> cluster id. ``truth``: observation -> org id."""
    clusters: dict[str, set[str]] = defaultdict(set)
    for obs, cluster in assignment.items():
        clusters[cluster].add(obs)
    orgs: dict[str, set[str]] = defaultdict(set)
    for obs, org in truth.items():
        if obs in assignment:
            orgs[org].add(obs)
    p_sum = r_sum = 0.0
    kind_r: dict[str, list[float]] = defaultdict(list)
    for obs, cluster in assignment.items():
        members, true = clusters[cluster], orgs[truth[obs]]
        inter = len(members & true)
        p_sum += inter / len(members)
        recall = inter / len(true)
        r_sum += recall
        kind_r[kind[truth[obs]]].append(recall)
    n = len(assignment) or 1
    precision, recall = p_sum / n, r_sum / n
    bad = [m for m in clusters.values() if len({truth[x] for x in m}) > 1]
    contaminated = sum(len(m) - Counter(truth[x] for x in m).most_common(1)[0][1] for m in bad)
    mixed: Counter[str] = Counter()
    for m in clusters.values():
        per_kind: dict[str, set[str]] = defaultdict(set)
        for x in m:
            per_kind[kind[truth[x]]].add(truth[x])
        for k, ids in per_kind.items():
            if len(ids) > 1:
                mixed[k] += 1
    return ClusterMetrics(
        observations=len(assignment),
        clusters=len(clusters),
        b3_precision=precision,
        b3_recall=recall,
        b3_f1=2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        false_merge_clusters=len(bad),
        contaminated=contaminated,
        largest_bad_cluster=max((len(m) for m in bad), default=0),
        by_kind_recall={k: sum(v) / len(v) for k, v in sorted(kind_r.items())},
        mixed_by_kind=dict(sorted(mixed.items())),
    )


def candidate_metrics(
    pairs: Iterable[tuple[str, str]],
    truth: Mapping[str, str],
    kind: Mapping[str, str] | None = None,
) -> dict:
    """Pair recall and entity connectivity of a candidate set (design 03 §5), with
    connectivity per organization kind when ``kind`` is given."""
    pairs = set(pairs)
    orgs: dict[str, list[str]] = defaultdict(list)
    for obs, org in truth.items():
        orgs[org].append(obs)
    true_pairs = {
        (a, b) if a < b else (b, a)
        for members in orgs.values()
        for a, b in combinations(members, 2)
    }
    found = true_pairs & pairs
    by_obs: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for p in pairs:
        if truth.get(p[0]) is not None and truth.get(p[0]) == truth.get(p[1]):
            by_obs[truth[p[0]]].add(p)
    is_connected = {
        org: len(components(members, by_obs.get(org, ()))) == 1 for org, members in orgs.items()
    }
    connected = sum(is_connected.values())
    per_kind: dict[str, list[bool]] = defaultdict(list)
    for org, ok in is_connected.items():
        if kind is not None:
            per_kind[kind[org]].append(ok)
    return {
        "connectivity_by_kind": {k: sum(v) / len(v) for k, v in sorted(per_kind.items())},
        "candidate_pairs": len(pairs),
        "true_pairs": len(true_pairs),
        "pair_recall": len(found) / len(true_pairs) if true_pairs else 1.0,
        "entity_connectivity": connected / len(orgs) if orgs else 1.0,
    }


@dataclass(frozen=True)
class Report:
    benchmark: dict[str, Any]
    clusters: ClusterMetrics
    candidates: dict[str, Any]
    reviews_opened: int
    review_observations: int
    seconds: dict[str, float]
    run: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        c = self.clusters
        return (
            f"{self.benchmark['records']} records: B3 P {c.b3_precision:.4f} R {c.b3_recall:.4f} "
            f"F1 {c.b3_f1:.4f} | false-merge clusters {c.false_merge_clusters} "
            f"(contaminated {c.contaminated}, largest {c.largest_bad_cluster}) | "
            f"reviews {self.reviews_opened}, {self.review_observations} obs in review | "
            f"candidates: pair recall {self.candidates['pair_recall']:.3f}, connected "
            f"{self.candidates['entity_connectivity']:.3f} | "
            f"T_high {self.run.get('t_high', float('nan')):.1f} | "
            f"{sum(self.seconds.values()):.1f}s"
        )


def run_benchmark(
    benchmark: Benchmark,
    *,
    profile: ResolutionProfile | None = None,
    random_pairs: int = 300_000,
    engine_kwargs: Mapping[str, Any] | None = None,
    seed: int = 7,
    adjudicator: Callable[[Thisness, dict[str, str], Any], Any] | None = None,
    plan: Callable[[Thisness, dict[str, str], Any], Any] | None = None,
    store: str = ":memory:",
) -> tuple[Report, Thisness, dict[str, str]]:
    """Ingest the benchmark into a fresh in-memory engine, resolve it as one bulk run,
    and measure. Returns the report, the engine, and observation id -> true entity id.
    ``adjudicator(engine, truth, benchmark)`` builds an adjudicator for the run, so a
    simulated one can read the ground truth (design 05 §7). ``plan(engine, truth,
    benchmark)`` builds a run plan the same way, such as a name-only plan (§6). ``store``
    keeps the run in a DuckDB file, so the Workbench can open it afterwards."""
    now = datetime(2026, 10, 7, tzinfo=UTC)
    ids = IdFactory.seeded(seed)
    entity_type = benchmark.entity_type
    engine = Thisness(
        store,
        profiles=[profile or PROFILES[entity_type]()],
        ids=ids,
        clock=lambda: now,
        random_pairs=random_pairs,
        **dict(engine_kwargs or {}),
    )
    seconds: dict[str, float] = {}
    started = time.perf_counter()
    observations = [
        engine.observe(
            entity_type,
            r.attributes,
            {"type": getattr(r, "source", "benchmark"), "id": r.record_id},
            ingested_at=now,
        )
        for r in benchmark.records
    ]
    engine.ingest(observations)
    seconds["ingest"] = time.perf_counter() - started
    truth = {
        o.observation_id: benchmark.truth[r.record_id]
        for o, r in zip(observations, benchmark.records, strict=True)
    }

    if adjudicator is not None:
        engine.adjudicator = adjudicator(engine, truth, benchmark)
    started = time.perf_counter()
    run_plan = plan(engine, truth, benchmark) if plan is not None else None
    result: RunResult = engine.resolve_many(entity_type, plan=run_plan)
    seconds["fit_and_resolve"] = time.perf_counter() - started

    assignment = engine.store.entities_of(list(truth))
    clusters = cluster_metrics(assignment, truth, benchmark.kind)
    candidates = (_candidates_of(engine, result, truth, benchmark.kind) if run_plan is None
                  else {"pair_recall": float("nan"), "entity_connectivity": float("nan")})  # fmt: skip
    review_obs = sum(1 for o in result.outcomes.values() if o.outcome == "REQUIRES_REVIEW")
    report = Report(
        benchmark=benchmark.config,
        clusters=clusters,
        candidates=candidates,
        reviews_opened=len(result.reviews),
        review_observations=review_obs,
        seconds=seconds,
        run={k: v for k, v in result.stats.items() if k != "candidate_stage"},
    )
    if "adjudication" in report.run:
        report.run["adjudication"] = {
            **report.run["adjudication"],
            "vs_truth": adjudication_accuracy(engine, truth, benchmark, result.run_id),
        }
    return report, engine, truth


def adjudication_accuracy(
    engine: Thisness, truth: Mapping[str, str], benchmark: Benchmark, run_id: str | None
) -> dict[str, Any]:
    """The model's answers against the benchmark's truth (design 06 §10.6): what a real
    model gets right, and how often it says "different" about one entity, the error that
    holds a whole component in decide mode. A pair of different entities in one group
    (a company group, a genus) is "related"; "different" is a right answer there too."""
    from thisness.benchmark.oracle import benchmark_truth

    truth_of = benchmark_truth(engine, truth)
    confusion: Counter[tuple[str, str]] = Counter()
    for judgment in engine.store.decisions_of_type("pair"):
        if judgment.actor.kind != "agent" or (run_id is not None and judgment.run_id != run_id):
            continue
        answer = next((str(s.value) for s in judgment.scores if s.kind == "llm_decision"), None)
        a, b = truth_of(judgment.left_id), truth_of(judgment.right_id)
        if answer is None or a is None or b is None:
            continue
        group_a, group_b = benchmark.group.get(a), benchmark.group.get(b)
        actual = ("same" if a == b else
                  "related" if group_a is not None and group_a == group_b else "different")  # fmt: skip
        confusion[(actual, answer)] += 1
    right = {("same", "same"), ("different", "different"), ("related", "related"),
             ("related", "different")}  # fmt: skip
    decided = {k: n for k, n in confusion.items() if k[1] != "unsure"}
    same_pairs = sum(n for (actual, _), n in decided.items() if actual == "same")
    other_pairs = sum(n for (actual, _), n in decided.items() if actual != "same")
    total = sum(decided.values())

    def rate(n: int, d: int) -> float | None:
        return round(n / d, 4) if d else None

    # Of the pairs that are one entity, answered "different" or "related": in decide
    # mode each is a negative edge inside a true entity.
    apart = sum(n for (a, x), n in decided.items() if a == "same" and x != "same")
    # Of those, answered "different": the only answer that sets a negative edge.
    split = sum(n for (a, x), n in decided.items() if a == "same" and x == "different")
    # Of the pairs that are not one entity, answered "same": a false merge if applied.
    merged = sum(n for (a, x), n in decided.items() if a != "same" and x == "same")
    return {
        "judged": sum(confusion.values()),
        "unsure": sum(n for (_, answer), n in confusion.items() if answer == "unsure"),
        "accuracy": rate(sum(n for k, n in decided.items() if k in right), total),
        "wrongly_apart": rate(apart, same_pairs),
        "wrongly_different": rate(split, same_pairs),
        "wrongly_same": rate(merged, other_pairs),
        "confusion": {f"{a}->{x}": n for (a, x), n in sorted(confusion.items())},
    }


def _candidates_of(
    engine: Thisness, result: RunResult, truth: Mapping[str, str], kind: Mapping[str, str]
) -> dict:
    from thisness.candidates import distinct_pairs, generate_all

    profile = engine.profiles[result.stats.get("entity_type") or next(iter(engine.profiles))]
    pairs = distinct_pairs(generate_all(engine.store, profile.candidate_generators, result.run_id))
    return candidate_metrics(pairs, truth, kind)


def recommendation_accuracy(engine: Thisness, truth: Mapping[str, str],
                            recommended: Iterable[Any]) -> dict[str, Any]:  # fmt: skip
    """How the review assistant's suggestions (design 05 §5) would fare if accepted:
    right when a suggestion's grouping is the truth's; wrong ones split into those that
    would merge different entities and those that would leave a match apart (a grouping
    can do both). A group's member that is an entity counts as its members' majority."""
    from thisness.benchmark.oracle import benchmark_truth

    truth_of = benchmark_truth(engine, truth)
    statuses: Counter[str] = Counter()
    by_resolution: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    unsure = merges_different = leaves_apart = 0
    for r in recommended:
        statuses[r.status] += 1
        rec = r.recommendation
        if rec is None:
            continue
        if rec.groups is None:
            unsure += 1
            continue
        groups = [[truth_of(n) for n in g] for g in rec.groups]
        mixed = any(len(set(g)) > 1 for g in groups)
        seen: Counter[str | None] = Counter(t for g in groups for t in set(g))
        apart = any(c > 1 for t, c in seen.items() if t is not None)
        merges_different += mixed
        leaves_apart += apart
        by_resolution[rec.resolution][0] += not (mixed or apart)
        by_resolution[rec.resolution][1] += 1
    right = sum(v[0] for v in by_resolution.values())
    decided = sum(v[1] for v in by_resolution.values())
    return {
        "reviews": sum(statuses.values()),
        "statuses": dict(statuses),
        "unsure": unsure,
        "decided": decided,
        "right": right,
        "accuracy": right / decided if decided else float("nan"),
        "by_resolution": {k: {"right": v[0], "of": v[1]} for k, v in sorted(by_resolution.items())},
        "would_merge_different": merges_different,
        "would_leave_apart": leaves_apart,
    }
