"""Sketch for docs/design/01-resolution-runs.md.

Not Thisness code. A stdlib-only check that the rules in the design doc
produce the outcomes the doc claims:

- per-stage votes, co-visibility, echo detection, label convergence (5.x)
- validated components: conflicts, load-bearing bridges, load-bearing
  articulation nodes (7.2)
- suggested cuts for reviewers, from a min-disagreement bipartition (D1)
- split proposals for existing entities, never automatic splits (D2)
- min_votes as a profile setting (D4)
- provisional entities and provisional merges (D5)
- variant collapse and anchored verify batches for large clusters (D6)

    python3 docs/design/sketches/consensus_sketch.py
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

# ---------------------------------------------------------------------------
# Inputs: stages and group proposals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    stage_id: str
    view: str  # "raw": producer saw source values. "derived": saw upstream labels.
    family: str  # producer family (model + prompt version, or method name)


@dataclass
class GroupProposal:
    stage_id: str
    batch_id: str
    shown: dict[str, str]  # node -> the string the producer was shown
    groups: list[set[str]]  # partition of a subset of shown; the rest are singletons
    labels_out: dict[str, str] = field(default_factory=dict)  # node -> proposed label


@dataclass
class Scenario:
    names: dict[str, str]
    stages: dict[str, Stage]
    proposals: list[GroupProposal]
    entities: dict[str, str] = field(default_factory=dict)  # node -> active | provisional
    entity_members: dict[str, list[str]] = field(default_factory=dict)  # for split checks
    nodes: list[str] | None = None  # consensus nodes; defaults to every named node


# ---------------------------------------------------------------------------
# Pair statistics
# ---------------------------------------------------------------------------


@dataclass
class PairStats:
    raw_votes: dict[str, bool] = field(default_factory=dict)  # stage -> co-assigned?
    derived_votes: dict[str, bool] = field(default_factory=dict)
    echoes: int = 0  # derived stage was shown identical upstream labels
    label_convergence: bool = False  # same proposed label from different batches

    @property
    def n(self) -> int:
        return len(self.raw_votes)

    @property
    def k(self) -> int:
        return sum(self.raw_votes.values())


def pair_stats(stages: dict[str, Stage], proposals: list[GroupProposal]) -> dict:
    # (pair, stage) -> verdicts across overlapping batches of that stage
    per_stage: dict[tuple, list[bool]] = defaultdict(list)
    echoes: dict[tuple, int] = defaultdict(int)
    labels: dict[tuple[str, str], tuple[str, str]] = {}  # (stage, node) -> (batch, label)

    for p in proposals:
        stage = stages[p.stage_id]
        group_of = {}
        for gi, g in enumerate(p.groups):
            for node in g:
                group_of[node] = gi
        for node in p.shown:
            group_of.setdefault(node, f"single:{node}")
        for node, label in p.labels_out.items():
            labels[(p.stage_id, node)] = (p.batch_id, label)
        for a, b in combinations(sorted(p.shown), 2):
            if stage.view == "derived" and p.shown[a] == p.shown[b]:
                echoes[(a, b)] += 1
                continue
            per_stage[((a, b), p.stage_id)].append(group_of[a] == group_of[b])

    stats: dict[tuple[str, str], PairStats] = defaultdict(PairStats)
    for (pair, stage_id), verdicts in per_stage.items():
        # One vote per stage per pair. Overlapping batches that disagree
        # within a stage count as a non-agreeing vote.
        vote = all(verdicts)
        if stages[stage_id].view == "raw":
            stats[pair].raw_votes[stage_id] = vote
        else:
            stats[pair].derived_votes[stage_id] = vote
    for pair, n in echoes.items():
        stats[pair].echoes = n

    by_stage = defaultdict(dict)
    for (stage_id, node), (batch, label) in labels.items():
        by_stage[stage_id][node] = (batch, label)
    for nodes in by_stage.values():
        for a, b in combinations(sorted(nodes), 2):
            (ba, la), (bb, lb) = nodes[a], nodes[b]
            if ba != bb and la == lb:
                stats[(a, b)].label_convergence = True
    return dict(stats)


# ---------------------------------------------------------------------------
# Edge policy (name-only profile, VoteEdgePolicy)
# ---------------------------------------------------------------------------


def classify(s: PairStats | None, cannot_link: bool, min_votes: int = 2) -> str:
    if cannot_link:
        return "cannot_link"
    if s is None:
        return "none"
    if s.n >= min_votes and s.k == s.n:
        return "strong"
    if s.n >= min_votes and s.k / s.n < 0.5:
        return "negative"
    if s.n >= min_votes:
        return "contested"  # mixed independent votes
    if s.k > 0 or any(s.derived_votes.values()) or s.label_convergence:
        return "unverified"
    return "none"


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


def components(nodes: list[str], edges: set[tuple[str, str]]) -> list[list[str]]:
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
                parent[max(ra, rb)] = min(ra, rb)  # deterministic root choice
    out = defaultdict(list)
    for n in sorted(nodes):
        out[find(n)].append(n)
    return sorted(out.values())


def load_bearing_bridges(comp: list[str], edges: set[tuple[str, str]]) -> list[tuple]:
    """Strong edges whose removal splits comp into two parts of size >= 2."""
    inner = {e for e in edges if e[0] in comp and e[1] in comp}
    found = []
    for e in sorted(inner):
        parts = components(comp, inner - {e})
        if len(parts) == 2 and min(len(parts[0]), len(parts[1])) >= 2:
            found.append((e, tuple(len(p) for p in parts)))
    return found


def load_bearing_articulations(comp: list[str], edges: set[tuple[str, str]]) -> list[tuple]:
    """Nodes whose removal leaves two or more parts of size >= 2.

    The node-shaped chain error: one shared node holding groups together that
    have no strong edge between them. A hub with single-observation leaves is
    not flagged.
    """
    inner = {e for e in edges if e[0] in comp and e[1] in comp}
    found = []
    for v in comp:
        rest = [n for n in comp if n != v]
        parts = components(rest, {e for e in inner if v not in e})
        big = sorted((len(p) for p in parts if len(p) >= 2), reverse=True)
        if len(big) >= 2:
            found.append((v, tuple(big)))
    return found


def suggest_cut(comp: list[str], edge, limit: int = 14):
    """Min-disagreement two-way cut, offered to a reviewer, never applied.

    Only meaningful when the component holds contradicting evidence: with
    positive edges alone, the minimum-disagreement answer is "do not cut".
    """
    if len(comp) > limit:
        return None
    rest = comp[1:]
    best = None
    for mask in range(1, 2 ** len(rest)):
        side_b = {rest[i] for i in range(len(rest)) if mask >> i & 1}
        cost = 0
        for a, b in combinations(comp, 2):
            kind = edge(a, b)
            same = (a in side_b) == (b in side_b)
            if kind == "strong" and not same:
                cost += 1
            elif kind == "negative" and same:
                cost += 1
            elif kind == "cannot_link" and same:
                cost += 100
        side_a = tuple(n for n in comp if n not in side_b)
        small, large = sorted((side_a, tuple(sorted(side_b))), key=lambda s: (len(s), s))
        key = (cost, len(small), small)
        if best is None or key < best[0]:
            best = (key, large, small)
    (cost, _, _), large, small = best
    return {"keep": list(large), "move": list(small), "disagreements": cost}


# ---------------------------------------------------------------------------
# Consensus: validated components
# ---------------------------------------------------------------------------


def outcome_for(comp: list[str], entities: dict[str, str]) -> tuple[str, str | None]:
    active = [n for n in comp if entities.get(n) == "active"]
    provisional = [n for n in comp if entities.get(n) == "provisional"]
    observations = [n for n in comp if n not in entities]
    if len(active) >= 2:
        return "PROPOSE_MERGE", None  # two confirmed identities: always review
    if provisional and (active or len(provisional) >= 2):
        survivor = active[0] if active else provisional[0]
        return "MERGE_PROVISIONAL", survivor
    if active or provisional:
        target = (active or provisional)[0]
        return ("ATTACH", target) if observations else ("NO_CHANGE", target)
    return "CREATE_ENTITY", None


def consensus(
    nodes, stats, *, entities=None, cannot_links=frozenset(), min_votes=2, check_articulation=True
):
    entities = entities or {}
    nodes = sorted(nodes)

    def edge(a, b):
        key = tuple(sorted((a, b)))
        return classify(stats.get(key), key in cannot_links, min_votes)

    strong = {p for p in combinations(nodes, 2) if edge(*p) == "strong"}
    results = []
    for comp in components(nodes, strong):
        r = {
            "members": comp,
            "conflicts": [
                (a, b, edge(a, b))
                for a, b in combinations(comp, 2)
                if edge(a, b) in ("negative", "cannot_link")
            ],
            "bridges": load_bearing_bridges(comp, strong),
            "articulations": load_bearing_articulations(comp, strong) if check_articulation else [],
            "suggestion": None,
            "target": None,
            "pending": 0,
        }
        if r["conflicts"] or r["bridges"] or r["articulations"]:
            r["outcome"] = "REVIEW_CLUSTER"
            if r["conflicts"]:
                r["suggestion"] = suggest_cut(comp, edge)
        else:
            r["outcome"], r["target"] = outcome_for(comp, entities)
        results.append(r)

    cluster_of = {n: i for i, r in enumerate(results) for n in r["members"]}
    boundaries = defaultdict(list)
    for a, b in combinations(nodes, 2):
        ca, cb = cluster_of[a], cluster_of[b]
        if ca == cb:
            continue
        kind = edge(a, b)
        if kind == "unverified":
            # A positive signal nobody has confirmed: the boundary is not settled.
            results[ca]["pending"] += 1
            results[cb]["pending"] += 1
        elif kind == "contested":
            # Independent votes disagree: an identity-or-relationship question.
            boundaries[(min(ca, cb), max(ca, cb))].append((a, b, kind))

    for r in results:
        # End-of-run hold rule (D5): an unsettled new cluster still gets an ID,
        # but a provisional one, so a later merge needs no review.
        if r["outcome"] == "CREATE_ENTITY" and r["pending"]:
            r["outcome"] = "CREATE_PROVISIONAL"
    return results, dict(boundaries)


def integrity(entity_members, stats, min_votes=2):
    """Split proposals for existing entities (D2). The entity is never changed."""
    out = []
    for ent, members in sorted(entity_members.items()):
        members = sorted(members)

        def edge(a, b):
            return classify(stats.get(tuple(sorted((a, b)))), False, min_votes)

        conflicts = [(a, b) for a, b in combinations(members, 2) if edge(a, b) == "negative"]
        if conflicts:
            out.append(
                {"entity": ent, "conflicts": conflicts, "suggestion": suggest_cut(members, edge)}
            )
    return out


# ---------------------------------------------------------------------------
# Display and driver
# ---------------------------------------------------------------------------


def fmt(members, names, cap=6):
    labels = [names[n] for n in members]
    if len(labels) > cap:
        return ", ".join(labels[:3]) + f", ... ({len(labels)} members)"
    return ", ".join(labels)


def show(title, names, results, boundaries, splits=()):
    print(f"\n=== {title}")
    for r in results:
        extra = ""
        if r["target"] and r["outcome"] in ("ATTACH", "MERGE_PROVISIONAL"):
            extra += f"  -> into {names[r['target']]}"
        if r["pending"]:
            extra += f"  ({r['pending']} unverified links pending)"
        print(f"  {r['outcome']:<19} [{fmt(r['members'], names)}]{extra}")
        for a, b, kind in r["conflicts"][:3]:
            print(f"      conflict: {names[a]} / {names[b]} ({kind})")
        for (a, b), sizes in r["bridges"][:3]:
            print(f"      bridge: {names[a]} / {names[b]} splits {sizes}")
        for v, sizes in r["articulations"][:3]:
            print(f"      articulation: {names[v]} holds together parts {sizes}")
        if r["suggestion"]:
            s = r["suggestion"]
            print(f"      suggested cut: [{fmt(s['keep'], names)}] | [{fmt(s['move'], names)}]")
    for (i, j), pairs in boundaries.items():
        a, b, _ = pairs[0]
        print(
            f"  BOUNDARY_REVIEW     cluster {i} <-> {j}: {len(pairs)} contested pairs, "
            f"e.g. {names[a]} / {names[b]}"
        )
    for s in splits:
        cut = s["suggestion"]
        print(f"  SPLIT_REVIEW        {names[s['entity']]} (entity unchanged)")
        print(f"      suggested: [{fmt(cut['keep'], names)}] | [{fmt(cut['move'], names)}]")


def evaluate(sc: Scenario, min_votes=2, check_articulation=True, proposals=None):
    proposals = sc.proposals if proposals is None else proposals
    nodes = sc.nodes if sc.nodes is not None else list(sc.names)
    stats = pair_stats(sc.stages, proposals)
    results, boundaries = consensus(
        nodes,
        stats,
        entities=sc.entities,
        min_votes=min_votes,
        check_articulation=check_articulation,
    )
    splits = integrity(sc.entity_members, stats, min_votes)
    return stats, results, boundaries, splits


def run(title, sc: Scenario, min_votes=2, check_articulation=True, shuffles=50):
    stats, results, boundaries, splits = evaluate(sc, min_votes, check_articulation)
    show(title, sc.names, results, boundaries, splits)
    if shuffles:
        rng = random.Random(7)
        for _ in range(shuffles):
            shuffled = sc.proposals[:]
            rng.shuffle(shuffled)
            _, r2, b2, s2 = evaluate(sc, min_votes, check_articulation, proposals=shuffled)
            assert (r2, b2, s2) == (results, boundaries, splits), "order dependence"
        print(f"  (same result under {shuffles} shuffles of proposal order)")
    return stats


# ---------------------------------------------------------------------------
# Scenario 1: the appendix's Amazon / AWS example
# ---------------------------------------------------------------------------


def scenario_amazon() -> Scenario:
    names = {
        "E1": "Amazon.com, Inc. [active]",
        "o1": "Amazon",
        "o2": "Amazon.com Inc.",
        "o3": "Amazon - 008712",
        "o4": "Amazon Web Services",
        "o5": "Amazon Web Services LLC",
        "o6": "AWS",
        "o7": "AWS 8721",
        "o8": "Amazon Bank",
    }
    stages = {
        # Pass 1: raw names, alphabetical. In a 60k list, AWS sorts far from Amazon.
        "p1": Stage("p1", "raw", "llm:grouper-v1"),
        # Pass 2: chained. Shown pass-1 labels, re-sorted by label.
        "p2": Stage("p2", "derived", "llm:grouper-v1"),
        # Verify: raw names, batches built from proposed clusters + nearest non-members.
        "vA": Stage("vA", "raw", "llm:verifier-v1"),
        "vB": Stage("vB", "raw", "llm:verifier-v1"),
    }

    def raw(ids):
        return {i: names[i] for i in ids}

    proposals = [
        GroupProposal(
            "p1",
            "p1-b1",
            raw(["E1", "o1", "o2", "o3", "o4", "o5", "o8"]),
            [{"E1", "o1", "o2", "o3"}, {"o4", "o5"}],
            {
                "E1": "Amazon",
                "o1": "Amazon",
                "o2": "Amazon",
                "o3": "Amazon",
                "o4": "Amazon Web Services",
                "o5": "Amazon Web Services",
                "o8": "Amazon Bank",
            },
        ),
        GroupProposal(
            "p1",
            "p1-b2",
            raw(["o6", "o7"]),
            [{"o6", "o7"}],
            {"o6": "Amazon Web Services", "o7": "Amazon Web Services"},
        ),
        GroupProposal(
            "p2",
            "p2-b1",
            {
                "E1": "Amazon",
                "o1": "Amazon",
                "o2": "Amazon",
                "o3": "Amazon",
                "o4": "Amazon Web Services",
                "o5": "Amazon Web Services",
                "o6": "Amazon Web Services",
                "o7": "Amazon Web Services",
                "o8": "Amazon Bank",
            },
            [{"E1", "o1", "o2", "o3"}, {"o4", "o5", "o6", "o7"}],
        ),
        GroupProposal(
            "vA",
            "vA-b1",
            raw(["E1", "o1", "o2", "o3", "o4", "o5", "o6", "o7", "o8"]),
            [{"E1", "o1", "o2", "o3"}, {"o4", "o5", "o6", "o7"}],
        ),
        # vB is the appendix's dissenting pass: it folds AWS into Amazon.
        GroupProposal(
            "vB",
            "vB-b1",
            raw(["E1", "o1", "o2", "o3", "o4", "o5", "o6", "o7", "o8"]),
            [{"E1", "o1", "o2", "o3", "o4", "o5", "o6", "o7"}],
        ),
    ]
    return Scenario(names, stages, proposals, entities={"E1": "active"})


# ---------------------------------------------------------------------------
# Scenario 2: chain error from overlapping batches in one pass
# ---------------------------------------------------------------------------


def scenario_chain() -> Scenario:
    names = {"x1": "Apple Inc.", "x2": "Apple", "x3": "Apple Bank", "x4": "Apple Bank NA"}
    stages = {
        "p1": Stage("p1", "raw", "llm:grouper-v1"),
        "p2": Stage("p2", "raw", "llm:grouper-v1"),
        "v1": Stage("v1", "raw", "llm:verifier-v1"),
    }

    def raw(ids):
        return {i: names[i] for i in ids}

    proposals = [
        # Overlapping windows: x2 sits in both batches and gets pulled both ways.
        GroupProposal("p1", "p1-b1", raw(["x1", "x2"]), [{"x1", "x2"}]),
        GroupProposal("p1", "p1-b2", raw(["x2", "x3", "x4"]), [{"x2", "x3", "x4"}]),
        GroupProposal("p2", "p2-b1", raw(["x1", "x2"]), [{"x1", "x2"}]),
        GroupProposal("p2", "p2-b2", raw(["x2", "x3", "x4"]), [{"x2", "x3", "x4"}]),
        # Seen together, x1 and the banks are kept apart.
        GroupProposal("v1", "v1-b1", raw(["x1", "x3", "x4"]), [{"x3", "x4"}]),
        GroupProposal("p2", "p2-b3", raw(["x1", "x3"]), []),
    ]
    return Scenario(names, stages, proposals)


# ---------------------------------------------------------------------------
# Scenario 3: a load-bearing bridge with no contradicting evidence
# ---------------------------------------------------------------------------


def scenario_bridge() -> Scenario:
    names = {
        "a": "J. Smith (Acme)",
        "b": "John Smith (Acme)",
        "c": "John Smith (Globex)",
        "d": "Jon Smith (Globex)",
    }
    stages = {"s1": Stage("s1", "raw", "fs:person-v1"), "s2": Stage("s2", "raw", "llm:grouper-v1")}
    proposals = [
        GroupProposal(s, f"{s}-{i}", {n: names[n] for n in ids}, [set(ids)])
        for s in ("s1", "s2")
        for i, ids in enumerate([["a", "b"], ["c", "d"], ["b", "c"]])
    ]
    return Scenario(names, stages, proposals)


# ---------------------------------------------------------------------------
# Scenario 4: the next run after scenario 1 ran without verify (D5)
# ---------------------------------------------------------------------------


def scenario_provisional_followup() -> Scenario:
    names = {
        "E1": "Amazon.com, Inc. [active]",
        "P1": "Amazon [provisional]",
        "P2": "Amazon.com Inc. [provisional]",
        "P3": "Amazon - 008712 [provisional]",
        # An older load that was never deduplicated: two confirmed identities.
        "E5": "Amazon Web Services LLC [active]",
        "E6": "AWS [active]",
        "P4": "AWS 8721 [provisional]",
        "o9": "AMAZON.COM INC",  # new observation in this run
    }
    stages = {
        "vA": Stage("vA", "raw", "llm:verifier-v1"),
        "vB": Stage("vB", "raw", "llm:verifier-v1"),
    }
    shown = {n: names[n].split(" [")[0] for n in names}
    groups = [{"E1", "P1", "P2", "P3", "o9"}, {"E5", "E6", "P4"}]
    proposals = [GroupProposal(s, f"{s}-b1", shown, groups) for s in ("vA", "vB")]
    entities = {
        "E1": "active",
        "E5": "active",
        "E6": "active",
        "P1": "provisional",
        "P2": "provisional",
        "P3": "provisional",
        "P4": "provisional",
    }
    return Scenario(names, stages, proposals, entities=entities)


# ---------------------------------------------------------------------------
# Scenario 5: verify evidence contradicts an existing entity's members (D2)
# ---------------------------------------------------------------------------


def scenario_split_proposal() -> Scenario:
    names = {
        "E2": "Acme Corp [active]",
        "m1": "Acme Corp",
        "m2": "ACME Corporation",
        "m3": "Acme Bank",
    }
    stages = {
        "vA": Stage("vA", "raw", "llm:verifier-v1"),
        "vB": Stage("vB", "raw", "llm:verifier-v1"),
    }
    shown = {n: names[n] for n in ("m1", "m2", "m3")}
    proposals = [GroupProposal(s, f"{s}-b1", shown, [{"m1", "m2"}]) for s in ("vA", "vB")]
    # Members of an existing entity are not consensus nodes: the entity is atomic.
    return Scenario(
        names,
        stages,
        proposals,
        entities={"E2": "active"},
        entity_members={"E2": ["m1", "m2", "m3"]},
        nodes=["E2"],
    )


# ---------------------------------------------------------------------------
# Scenario 6: clusters larger than one verify batch (D6)
# ---------------------------------------------------------------------------

LEGAL_TOKENS = {"INC", "LLC", "CORP", "CO", "LTD"}


def normalize(name: str) -> str:
    tokens = re.split(r"[^A-Z0-9]+", name.upper())
    return "".join(t for t in tokens if t and t not in LEGAL_TOKENS)


def large_raw_names() -> list[str]:
    prefixes = ("Amazon", "AMAZON", "Amazon.com", "Amazon com", "Amazon.Com")
    legal = ("", " Inc", " Inc.", ", Inc.")
    accounts = [""] + [f" - {n}" for n in range(1001, 1015)]
    return [f"{p}{lg}{a}" for p in prefixes for lg in legal for a in accounts]


def scenario_large(plan: str, batch: int = 12) -> Scenario:
    reps: dict[str, str] = {}
    for name in large_raw_names():
        reps.setdefault(normalize(name), name)  # first raw spelling represents the key
    names = {f"r{i:02d}": reps[k] for i, k in enumerate(sorted(reps))}
    names["bank"] = "Amazon Bank"
    ids = [n for n in names if n != "bank"]

    stages = {
        "p1": Stage("p1", "raw", "llm:grouper-v1"),
        "vA": Stage("vA", "raw", "llm:verifier-v1"),
        "vB": Stage("vB", "raw", "llm:verifier-v1"),
    }
    proposals = []
    # Pass 1: alphabetical windows of `batch`, everything labeled "Amazon".
    for i in range(0, len(ids), batch):
        chunk = ids[i : i + batch]
        proposals.append(
            GroupProposal(
                "p1",
                f"p1-{i}",
                {n: names[n] for n in chunk},
                [set(chunk)],
                {n: "Amazon" for n in chunk},
            )
        )

    # Verify: sub-batches of `batch` including the distractor and any anchors.
    anchors = {"partition": [], "one_anchor": ids[:1], "two_anchors": ids[:2]}[plan]
    others = [n for n in ids if n not in anchors]
    room = batch - 1 - len(anchors)
    for s in ("vA", "vB"):
        for i in range(0, len(others), room):
            members = anchors + others[i : i + room]
            shown = {n: names[n] for n in members + ["bank"]}
            proposals.append(GroupProposal(s, f"{s}-{i}", shown, [set(members)]))
    return Scenario(names, stages, proposals)


# ---------------------------------------------------------------------------


if __name__ == "__main__":
    stats = run("Scenario 1: Amazon / AWS", scenario_amazon())
    s = stats[("o4", "o6")]
    print(
        f"  AWS / Amazon Web Services: raw votes {s.k}/{s.n}, echoes {s.echoes}, "
        f"label convergence {s.label_convergence}"
    )
    s = stats[("o1", "o4")]
    print(f"  Amazon / Amazon Web Services: raw votes {s.k}/{s.n}")

    sc = scenario_amazon()
    sc.proposals = [p for p in sc.proposals if p.stage_id in ("p1", "p2")]
    run("Scenario 1 without verify, min_votes=2 (default)", sc)
    run("Scenario 1 without verify, min_votes=1", sc, min_votes=1)

    run("Scenario 2: chain through overlapping batches", scenario_chain())
    run("Scenario 3: one strong edge joins two pairs", scenario_bridge())
    run(
        "Scenario 4: next run, provisional entities from scenario 1",
        scenario_provisional_followup(),
    )
    run("Scenario 5: verify contradicts an existing entity", scenario_split_proposal())

    raw_names = large_raw_names()
    collapsed = {normalize(n) for n in raw_names}
    print("\n=== Scenario 6: large cluster, verify batch of 12 (incl. one distractor)")
    print(f"  variant collapse: {len(raw_names)} raw names -> {len(collapsed)} representatives")
    run("Scenario 6a: plain partition into sub-batches", scenario_large("partition"), shuffles=10)
    run(
        "Scenario 6b: one shared anchor, articulation check off",
        scenario_large("one_anchor"),
        check_articulation=False,
        shuffles=10,
    )
    run("Scenario 6c: one shared anchor", scenario_large("one_anchor"), shuffles=10)
    run("Scenario 6d: two shared anchors", scenario_large("two_anchors"), shuffles=10)
