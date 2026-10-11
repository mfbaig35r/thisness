"""Consensus: validated components with holdout (design 01 §7, design 03 §10).

Input is a graph of nodes (observations in scope, and existing entities) whose
edges carry classes decided by a profile's edge policy. Consensus never sees
scores or evidence, only classes.

1. Components over strong edges.
2. A component is accepted if it has no internal negative or cannot-link pair, no
   load-bearing bridge and no load-bearing articulation node (each splits it into
   two or more parts of at least ``min_part`` nodes).
3. A component that fails only on conflicts may hold a greedy conflict cover of at
   most ``holdout_share`` of its nodes and accept what remains. Held nodes are
   merged nowhere.
4. A component that fails only on structure is split at its weak points: a
   load-bearing articulation node is held, a load-bearing bridge is cut, and each
   remaining part is validated on its own. Nothing is merged across a weak point;
   a boundary review asks whether the parts are one entity.
5. Accepted components map to outcomes by the entity states they contain.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import combinations
from typing import Literal

from thisness.core.components import ComponentRef
from thisness.core.decisions import ConsensusOutcome

EdgeClass = Literal["strong", "negative", "contested", "unverified", "cannot_link"]
_CONFLICT = frozenset({"negative", "cannot_link"})


@dataclass(frozen=True)
class Cluster:
    members: tuple[str, ...]
    outcome: ConsensusOutcome
    target: str | None = None
    conflicts: tuple[tuple[str, str, str], ...] = ()
    bridges: tuple[tuple[str, str, int, int], ...] = ()
    articulations: tuple[tuple[str, tuple[int, ...]], ...] = ()
    held_out: bool = False  # held by holdout or at a weak point; the rest was accepted
    linked: tuple[str, ...] = ()  # a held node's strong neighbors, shown to the reviewer
    pending: int = 0  # unverified links leaving the cluster
    suggested_cut: tuple[tuple[str, ...], tuple[str, ...]] | None = None


@dataclass(frozen=True)
class Boundary:
    """A neighborhood of accepted clusters joined by contested edges: one review asking
    whether they are one entity, several, or related ones. Grouping by neighborhood
    asks one question about 25 clusters sharing a common name, not 300."""

    clusters: tuple[int, ...]
    pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ConsensusResult:
    clusters: tuple[Cluster, ...]
    boundaries: tuple[Boundary, ...] = field(default_factory=tuple)
    unasked_pairs: int = 0  # contested pairs left out of every review by max_boundary

    def cluster_of(self) -> dict[str, int]:
        return {n: i for i, c in enumerate(self.clusters) for n in c.members}


def _key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def components(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in sorted(edges):
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    groups: dict[str, list[str]] = defaultdict(list)
    for n in sorted(parent):
        groups[find(n)].append(n)
    return sorted(groups.values())


def _bounded_groups(
    contested: list[tuple[int, int, str, str]],
    limit: int | None,
    strength: Mapping[tuple[str, str], float],
) -> list[set[int]]:
    """Clusters linked by contested pairs, as review groups of at most ``limit``
    clusters: pairs are taken strongest first, and one joins two groups only while the
    joined group stays within the limit (Kruskal's order, with a size bound)."""
    parent: dict[int, int] = {}
    size: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        size.setdefault(x, 1)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    order = sorted(contested, key=lambda c: (-strength.get(_key(c[2], c[3]), 0.0), c[2], c[3]))
    for i, j, _, _ in order:
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        if limit is not None and size[ri] + size[rj] > limit:
            continue
        parent[rj] = ri
        size[ri] += size[rj]
    groups: dict[int, set[int]] = {}
    for i, j, _, _ in contested:
        for x in (i, j):
            groups.setdefault(find(x), set()).add(x)
    # A cluster that joined no group (all its pairs went to full groups) asks nothing.
    return [g for g in groups.values() if len(g) > 1]


def weak_points(
    members: list[str], adjacency: Mapping[str, set[str]], min_part: int
) -> tuple[list[tuple[str, str, int, int]], list[tuple[str, tuple[int, ...]]]]:
    """Load-bearing bridges and articulation nodes of one connected component, in
    linear time (Tarjan, iterative). A bridge or node is load-bearing when removing
    it leaves at least two parts of ``min_part`` or more nodes."""
    n = len(members)
    if n < 2 * min_part:
        return [], []
    inside = set(members)
    disc: dict[str, int] = {}
    low: dict[str, int] = {}
    size: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    separated: dict[str, list[int]] = defaultdict(list)
    bridges = []
    root = members[0]
    disc[root] = low[root] = 0
    size[root] = 1
    parent[root] = None
    counter = 1
    stack = [(root, iter(sorted(adjacency[root] & inside)))]
    while stack:
        v, neighbors = stack[-1]
        advanced = False
        for w in neighbors:
            if w not in disc:
                disc[w] = low[w] = counter
                counter += 1
                size[w] = 1
                parent[w] = v
                stack.append((w, iter(sorted(adjacency[w] & inside))))
                advanced = True
                break
            if w != parent[v]:
                low[v] = min(low[v], disc[w])
        if advanced:
            continue
        stack.pop()
        p = parent[v]
        if p is None:
            continue
        size[p] += size[v]
        low[p] = min(low[p], low[v])
        if low[v] >= disc[p]:
            separated[p].append(size[v])
        if low[v] > disc[p] and min(size[v], n - size[v]) >= min_part:
            a, b = _key(p, v)
            bridges.append((a, b, size[v], n - size[v]))
    articulations = []
    for v in members:
        parts = list(separated.get(v, []))
        if v != root:
            rest = n - 1 - sum(parts)
            if rest > 0:
                parts.append(rest)
        big = sorted((s for s in parts if s >= min_part), reverse=True)
        if len(parts) >= 2 and len(big) >= 2:
            articulations.append((v, tuple(big)))
    return sorted(bridges), sorted(articulations)


def conflict_cover(conflicts: Iterable[tuple[str, str]]) -> list[str]:
    """Greedy vertex cover of the conflict graph: most conflicts first, ties by id."""
    remaining = {_key(a, b) for a, b in conflicts}
    cover: list[str] = []
    while remaining:
        degree = Counter(x for pair in remaining for x in pair)
        v = min(degree, key=lambda x: (-degree[x], x))
        cover.append(v)
        remaining = {p for p in remaining if v not in p}
    return cover


def suggested_cut(
    members: list[str], edge: Mapping[tuple[str, str], EdgeClass], limit: int = 14
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Minimum-disagreement two-way partition, for a reviewer. Never applied."""
    if len(members) > limit:
        return None
    rest = members[1:]
    best = None
    for mask in range(1, 2 ** len(rest)):
        side = {rest[i] for i in range(len(rest)) if mask >> i & 1}
        cost = 0
        for a, b in combinations(members, 2):
            kind = edge.get(_key(a, b))
            same = (a in side) == (b in side)
            if (kind == "strong" and not same) or (kind == "negative" and same):
                cost += 1
            elif kind == "cannot_link" and same:
                cost += 100
        a_side = tuple(m for m in members if m not in side)
        small, large = sorted((a_side, tuple(sorted(side))), key=lambda s: (len(s), s))
        key = (cost, len(small), small)
        if best is None or key < best[0]:
            best = (key, large, small)
    assert best is not None
    return best[1], best[2]


def outcome_for(
    members: Iterable[str], states: Mapping[str, str]
) -> tuple[ConsensusOutcome, str | None]:
    """Design 01 §7.4. ``states`` maps entity nodes to active or provisional; any
    other node is an observation."""
    members = list(members)
    active = sorted(n for n in members if states.get(n) == "active")
    provisional = sorted(n for n in members if states.get(n) == "provisional")
    observations = [n for n in members if n not in states]
    if len(active) >= 2:
        return ConsensusOutcome.PROPOSE_MERGE, None
    if provisional and (active or len(provisional) >= 2):
        return ConsensusOutcome.MERGE_PROVISIONAL, active[0] if active else provisional[0]
    if active or provisional:
        target = (active or provisional)[0]
        return (ConsensusOutcome.ATTACH if observations else ConsensusOutcome.NO_CHANGE), target
    return ConsensusOutcome.CREATE_ENTITY, None


@dataclass(frozen=True)
class ValidatedComponents:
    holdout_share: float = 0.10
    min_part: int = 2
    split_weak_points: bool = True
    # The most clusters one boundary review asks about (design 07 §13.4). Real look-alike
    # companies chain through contested pairs: one review held 419 entities, 38% of all
    # records under review. Larger chains are cut into reviews built from the strongest
    # contested pairs first; a pair that would join two full reviews is not asked, which
    # leaves the two apart. None: no limit.
    max_boundary: int | None = 12

    @property
    def ref(self) -> ComponentRef:
        config = {
            "holdout_share": self.holdout_share,
            "min_part": self.min_part,
            "split_weak_points": self.split_weak_points,
        }
        if self.max_boundary is None:  # the unlimited policy keeps its original ref
            return ComponentRef.of("consensus", "validated_components", "2", config)
        return ComponentRef.of("consensus", "validated_components", "3",
                               {**config, "max_boundary": self.max_boundary})  # fmt: skip

    def run(
        self,
        nodes: Iterable[str],
        edges: Mapping[tuple[str, str], EdgeClass],
        states: Mapping[str, str],
        strength: Mapping[tuple[str, str], float] | None = None,
    ) -> ConsensusResult:
        """``strength`` orders contested pairs when a boundary review must be cut (the
        evidence's weight for the pair); without it, pairs keep their sorted order."""
        nodes = sorted(set(nodes))
        edges = {_key(a, b): c for (a, b), c in edges.items()}
        strong = {pair for pair, c in edges.items() if c == "strong"}
        adjacency: dict[str, set[str]] = defaultdict(set)
        for a, b in strong:
            adjacency[a].add(b)
            adjacency[b].add(a)
        conflicts_of: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for pair, c in edges.items():
            if c in _CONFLICT:
                conflicts_of[pair[0]].append(pair)

        def internal_conflicts(members: list[str]) -> list[tuple[str, str, str]]:
            inside = set(members)
            return sorted(
                (a, b, edges[(a, b)])
                for m in members
                for a, b in conflicts_of.get(m, ())
                if b in inside
            )

        clusters: list[Cluster] = []
        severed: list[tuple[str, str]] = []  # bridges cut; reviewed as boundaries

        def settle(members: list[str], *, allow_holdout: bool) -> None:
            if len(members) == 1:
                clusters.append(Cluster(tuple(members), *outcome_for(members, states)))
                return
            conflicts = internal_conflicts(members)
            bridges, articulations = weak_points(members, adjacency, self.min_part)
            if not conflicts and not bridges and not articulations:
                clusters.append(Cluster(tuple(members), *outcome_for(members, states)))
                return
            # A conflict between two existing entities is never held out: entities are
            # settled identities, and holding one would hide the question, not answer it.
            entity_conflict = any(a in states and b in states for a, b, _ in conflicts)
            if (
                allow_holdout
                and conflicts
                and not entity_conflict
                and not bridges
                and not articulations
            ):
                cover = conflict_cover([(a, b) for a, b, _ in conflicts])
                if len(cover) <= self.holdout_share * len(members):
                    for v in sorted(cover):
                        clusters.append(
                            Cluster((v,), ConsensusOutcome.REVIEW_CLUSTER, held_out=True)
                        )
                    core = [m for m in members if m not in cover]
                    core_edges = [(a, b) for a, b in strong if a in core and b in core]
                    for piece in components(core, core_edges):
                        settle(piece, allow_holdout=False)
                    return
            if self.split_weak_points and not conflicts:
                held = sorted({v for v, _ in articulations})
                parts_of = dict(articulations)
                cut_edges = {(a, b) for a, b, _, _ in bridges}
                severed.extend(sorted(cut_edges))
                for v in held:
                    clusters.append(
                        Cluster(
                            (v,),
                            ConsensusOutcome.REVIEW_CLUSTER,
                            articulations=((v, parts_of[v]),),
                            held_out=True,
                            linked=tuple(sorted(adjacency[v] & set(members))),
                        )
                    )
                rest = [m for m in members if m not in held]
                rest_edges = [
                    (a, b) for a, b in strong if a in rest and b in rest and (a, b) not in cut_edges
                ]
                for piece in components(rest, rest_edges):
                    settle(piece, allow_holdout=allow_holdout)
                return
            cut = suggested_cut(members, edges) if conflicts else None
            clusters.append(
                Cluster(
                    tuple(members),
                    ConsensusOutcome.REVIEW_CLUSTER,
                    conflicts=tuple(conflicts),
                    bridges=tuple(bridges),
                    articulations=tuple(articulations),
                    suggested_cut=cut,
                )
            )

        for component in components(nodes, strong):
            settle(component, allow_holdout=True)

        # Unverified links leave a new cluster's boundary unsettled (D5): it gets a
        # provisional id. Contested links between accepted clusters ask a reviewer
        # whether they are one entity or related ones.
        index = {n: i for i, c in enumerate(clusters) for n in c.members}
        severed_set = set(severed)
        pending: Counter[int] = Counter()
        contested: list[tuple[int, int, str, str]] = []
        for (a, b), c in sorted(edges.items()):
            if a not in index or b not in index or index[a] == index[b]:
                continue
            ia, ib = index[a], index[b]
            if (a, b) in severed_set:
                contested.append((min(ia, ib), max(ia, ib), a, b))
            elif c == "unverified":
                pending[ia] += 1
                pending[ib] += 1
            elif c == "contested" and not (
                clusters[ia].outcome == ConsensusOutcome.REVIEW_CLUSTER
                or clusters[ib].outcome == ConsensusOutcome.REVIEW_CLUSTER
            ):
                contested.append((min(ia, ib), max(ia, ib), a, b))
        final = []
        for i, cluster in enumerate(clusters):
            outcome = cluster.outcome
            if outcome == ConsensusOutcome.CREATE_ENTITY and pending[i]:
                outcome = ConsensusOutcome.CREATE_PROVISIONAL
            final.append(
                Cluster(
                    cluster.members,
                    outcome,
                    cluster.target,
                    cluster.conflicts,
                    cluster.bridges,
                    cluster.articulations,
                    cluster.held_out,
                    cluster.linked,
                    pending[i],
                    cluster.suggested_cut,
                )
            )
        groups = _bounded_groups(contested, self.max_boundary,
                                 {_key(a, b): v for (a, b), v in (strength or {}).items()})  # fmt: skip
        boundaries = []
        unasked = 0
        for members in groups:
            pairs = sorted((a, b) for i, j, a, b in contested if i in members and j in members)
            boundaries.append(Boundary(tuple(sorted(members)), tuple(pairs)))
        grouped = {i: k for k, members in enumerate(groups) for i in members}
        unasked = sum(1 for i, j, _, _ in contested if grouped.get(i) != grouped.get(j))
        return ConsensusResult(tuple(final), tuple(sorted(boundaries, key=lambda b: b.clusters)),
                               unasked_pairs=unasked)  # fmt: skip
