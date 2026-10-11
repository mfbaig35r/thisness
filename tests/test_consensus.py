from __future__ import annotations

import random
from itertools import combinations, pairwise

import pytest

from thisness.consensus import ValidatedComponents, components, outcome_for, weak_points
from thisness.core.decisions import ConsensusOutcome as O

C = ValidatedComponents()


def run(edges, nodes=None, states=None, **kwargs):
    nodes = nodes or sorted({n for pair in edges for n in pair})
    consensus = ValidatedComponents(**kwargs) if kwargs else C
    return consensus.run(nodes, edges, states or {})


def by_members(result):
    return {c.members: c for c in result.clusters}


def strong_clique(names):
    return {pair: "strong" for pair in combinations(names, 2)}


def test_clean_component_creates_one_entity():
    result = run(strong_clique(["a", "b", "c"]))
    assert [(c.members, c.outcome) for c in result.clusters] == [(("a", "b", "c"), O.CREATE_ENTITY)]


def test_chain_with_contradiction_goes_to_review_with_a_suggested_cut():
    # Apple Inc. ~ Apple ~ Apple Bank, but Apple Inc. vs Apple Bank kept apart.
    edges = {("x1", "x2"): "strong", ("x2", "x3"): "strong", ("x2", "x4"): "strong",
             ("x3", "x4"): "strong", ("x1", "x3"): "negative"}  # fmt: skip
    (cluster,) = run(edges).clusters
    assert cluster.outcome == O.REVIEW_CLUSTER and not cluster.held_out
    assert cluster.conflicts == (("x1", "x3", "negative"),)
    assert cluster.suggested_cut == (("x2", "x3", "x4"), ("x1",))


def test_load_bearing_bridge_is_cut_and_reviewed_as_a_boundary():
    edges = {("a", "b"): "strong", ("c", "d"): "strong", ("b", "c"): "strong"}
    result = run(edges)
    assert {c.members: c.outcome for c in result.clusters} == {
        ("a", "b"): O.CREATE_ENTITY,
        ("c", "d"): O.CREATE_ENTITY,
    }
    (boundary,) = result.boundaries
    assert boundary.pairs == (("b", "c"),)


def test_single_shared_node_is_held_and_its_groups_accepted():
    edges = {**strong_clique(["anchor", "a1", "a2"]), **strong_clique(["anchor", "b1", "b2"])}
    result = by_members(run(edges))
    held = result[("anchor",)]
    assert held.outcome == O.REVIEW_CLUSTER and held.held_out
    assert held.articulations == (("anchor", (2, 2)),)
    assert held.linked == ("a1", "a2", "b1", "b2")
    assert result[("a1", "a2")].outcome == result[("b1", "b2")].outcome == O.CREATE_ENTITY


def test_without_splitting_weak_points_the_whole_component_is_reviewed():
    edges = {("a", "b"): "strong", ("c", "d"): "strong", ("b", "c"): "strong"}
    (cluster,) = run(edges, split_weak_points=False).clusters
    assert cluster.outcome == O.REVIEW_CLUSTER
    assert cluster.bridges == (("b", "c", 2, 2),)
    assert cluster.suggested_cut is None  # no contradicting evidence: show the weak point


def test_hub_with_single_leaves_is_normal():
    edges = {("hub", leaf): "strong" for leaf in "abcde"}
    (cluster,) = run(edges).clusters
    assert cluster.outcome == O.CREATE_ENTITY


def test_two_shared_anchors_are_accepted():
    edges = {**strong_clique(["s1", "s2", "a1", "a2"]), **strong_clique(["s1", "s2", "b1", "b2"])}
    (cluster,) = run(edges).clusters
    assert cluster.outcome == O.CREATE_ENTITY


def test_holdout_holds_outliers_of_a_large_component():
    names = [f"n{i:02d}" for i in range(20)]
    edges = strong_clique(names)
    # One typo'd record contradicts three others; it stays strongly linked to the rest.
    for other in ("n01", "n02", "n03"):
        edges[("n00", other)] = "negative"
    result = by_members(run(edges))
    assert result[("n00",)].outcome == O.REVIEW_CLUSTER and result[("n00",)].held_out
    assert result[tuple(names[1:])].outcome == O.CREATE_ENTITY


def test_holdout_never_applies_to_small_components():
    edges = strong_clique(["a", "b", "c", "d"])
    edges[("a", "b")] = "negative"
    (cluster,) = run(edges).clusters
    assert cluster.outcome == O.REVIEW_CLUSTER and not cluster.held_out


@pytest.mark.parametrize(
    ("states", "outcome", "target"),
    [
        ({}, O.CREATE_ENTITY, None),
        ({"E1": "active"}, O.ATTACH, "E1"),
        ({"E1": "active", "P1": "provisional"}, O.MERGE_PROVISIONAL, "E1"),
        ({"P2": "provisional", "P1": "provisional"}, O.MERGE_PROVISIONAL, "P1"),
        ({"E1": "active", "E2": "active"}, O.PROPOSE_MERGE, None),
    ],
)
def test_outcomes_by_entity_states(states, outcome, target):
    assert outcome_for(["obs_a", *states], states) == (outcome, target)


def test_entity_alone_is_no_change():
    assert outcome_for(["E1"], {"E1": "active"}) == (O.NO_CHANGE, "E1")


def test_unverified_links_make_new_clusters_provisional():
    edges = {("a", "b"): "strong", ("b", "c"): "unverified"}
    result = by_members(run(edges))
    assert result[("a", "b")].outcome == O.CREATE_PROVISIONAL
    assert result[("c",)].outcome == O.CREATE_PROVISIONAL
    assert result[("a", "b")].pending == 1


def test_boundaries_group_contested_neighborhoods():
    # Five separate "Premier Services" clusters, all contested with each other: one review.
    edges = {(f"p{i}", f"q{i}"): "strong" for i in range(5)}
    for i, j in combinations(range(5), 2):
        edges[(f"p{i}", f"p{j}")] = "contested"
    edges[("z1", "z2")] = "contested"  # an unrelated pair: its own review
    result = run(edges)
    assert len(result.boundaries) == 2
    sizes = sorted(len(b.clusters) for b in result.boundaries)
    assert sizes == [2, 5]


def test_results_do_not_depend_on_input_order():
    rng = random.Random(3)
    nodes = [f"n{i}" for i in range(30)]
    kinds = ["strong", "strong", "negative", "contested"]
    edges = {pair: rng.choice(kinds) for pair in combinations(nodes, 2) if rng.random() < 0.15}
    expected = run(edges, nodes)
    for _ in range(10):
        shuffled_nodes = nodes[:]
        rng.shuffle(shuffled_nodes)
        items = list(edges.items())
        rng.shuffle(items)
        assert run(dict(items), shuffled_nodes) == expected


# --- weak points: Tarjan against brute force --------------------------------------------------


def brute_force(members, edges, min_part):
    bridges, articulations = [], []
    for e in sorted(edges):
        parts = components(members, [x for x in edges if x != e])
        if len(parts) == 2 and min(len(p) for p in parts) >= min_part:
            bridges.append(e)
    for v in members:
        rest = [m for m in members if m != v]
        parts = components(rest, [x for x in edges if v not in x])
        big = sorted((len(p) for p in parts if len(p) >= min_part), reverse=True)
        if len(big) >= 2:
            articulations.append((v, tuple(big)))
    return bridges, articulations


@pytest.mark.parametrize("seed", range(150))
def test_tarjan_weak_points_match_brute_force(seed):
    rng = random.Random(seed)
    n = rng.randint(2, 14)
    nodes = [f"v{i:02d}" for i in range(n)]
    p = rng.choice([0.15, 0.25, 0.4])
    edges = {pair for pair in combinations(nodes, 2) if rng.random() < p}
    adjacency = {v: set() for v in nodes}
    for a, b in edges:
        adjacency[a].add(b)
        adjacency[b].add(a)
    for component in components(nodes, edges):
        inner = {e for e in edges if e[0] in component and e[1] in component}
        for min_part in (1, 2, 3):
            bridges, articulations = weak_points(component, adjacency, min_part)
            expected_bridges, expected_articulations = brute_force(component, inner, min_part)
            assert [(a, b) for a, b, _, _ in bridges] == expected_bridges
            assert articulations == expected_articulations


def test_a_conflict_between_two_entities_is_never_held_out():
    # A reviewer said E1 and E2 differ; new records link strongly to both. Holding one
    # entity out would hide the question, so the whole neighborhood goes to review.
    names = [f"n{i:02d}" for i in range(20)]
    edges = strong_clique([*names, "E1", "E2"])
    edges[("E1", "E2")] = "cannot_link"
    states = {"E1": "active", "E2": "active"}
    (cluster,) = run(edges, states=states).clusters
    assert cluster.outcome == O.REVIEW_CLUSTER and not cluster.held_out
    assert {"E1", "E2"} <= set(cluster.members)


def test_a_long_chain_of_contested_pairs_is_cut_into_answerable_reviews():
    """Real look-alike companies chained one review to 419 entities (design 07 §13.4)."""
    nodes = [f"n{i:02d}" for i in range(30)]
    edges = {(a, b): "contested" for a, b in pairwise(nodes)}
    # The pair n10-n11 is the strongest: it must stay inside one review.
    strength = {pair: 1.0 for pair in edges} | {("n10", "n11"): 9.0}
    result = ValidatedComponents(max_boundary=5).run(nodes, edges, {}, strength=strength)
    assert result.boundaries and all(len(b.clusters) <= 5 for b in result.boundaries)
    cluster_of = result.cluster_of()
    together = [b for b in result.boundaries
                if {cluster_of["n10"], cluster_of["n11"]} <= set(b.clusters)]  # fmt: skip
    assert len(together) == 1
    asked = sum(len(b.pairs) for b in result.boundaries)
    assert asked + result.unasked_pairs == len(edges) and result.unasked_pairs > 0
    unlimited = ValidatedComponents(max_boundary=None).run(nodes, edges, {})
    assert len(unlimited.boundaries) == 1 and unlimited.unasked_pairs == 0
    assert ValidatedComponents(max_boundary=None).ref.version == "2"  # the old policy's ref
