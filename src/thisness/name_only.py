"""Name-only resolution (design 05 §6, design 01 §6, PRD appendix A.9).

When a record carries nothing but a name, Fellegi-Sunter has one comparison to work
with. This run plan asks a model to group names instead, in overlapping batches,
and treats repeated independent agreement as evidence:

1. **Collapse.** Records whose names reduce to the same key (case, punctuation,
   legal form, appended account numbers) are one node, shown by one representative.
   A node whose key matches an existing entity's member joins that entity outright.
2. **Raw grouping pass.** Nodes in name order, in overlapping windows; the model
   groups each window and labels each group. One vote per pair for the stage: a pair
   two windows disagreed on is not co-assigned.
3. **Chained pass.** Nodes ordered by the first pass's labels and shown those labels:
   it finds pairs the first pass's windows kept apart ("AWS" and "Amazon Web
   Services"). Its votes are derived: they propose, they do not count. Two nodes shown
   the identical label are an echo, not a vote.
4. **Verify.** Every component of positive, uncontradicted edges is shown again with
   raw names and a few nearby distractors, twice with different distractors. Larger
   components are split into sub-batches that share two anchors.

``VoteEdgePolicy`` turns votes into edge classes (``min_votes`` raw votes, all
co-assigned, is strong), and validated-components consensus does the rest, exactly as
for any run. Co-assignment rates are typed scores, not probabilities.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from itertools import combinations
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from thisness.consensus import ConsensusResult
from thisness.core.components import ComponentRef
from thisness.core.decisions import Actor, ConsensusOutcome, PairJudgment, Verdict
from thisness.core.evidence import EvidenceItem, Score
from thisness.core.run import ChangeSet, GroupProposal, ProposalMember, ReadSet
from thisness.llm.client import BudgetExhausted, LLMClient
from thisness.llm.prompts import Prompt
from thisness.normalize.organization import LEGAL_FORMS

if TYPE_CHECKING:
    from thisness.engine import RunResult, Thisness

VERDICTS = {"strong": Verdict.MATCH, "negative": Verdict.NO_MATCH,
            "contested": Verdict.POSSIBLE_MATCH, "unverified": Verdict.POSSIBLE_MATCH,
            "cannot_link": Verdict.NO_MATCH}  # fmt: skip


class NameGroup(BaseModel):
    members: list[str]
    label: str | None = Field(default=None, max_length=200)
    uncertain: list[str] = Field(default_factory=list)


class GroupingAnswer(BaseModel):
    groups: list[NameGroup]


def default_prompt() -> Prompt:
    return Prompt.load("group_names", "2", GroupingAnswer)


@dataclass
class NameOnlyPlan:
    client: LLMClient
    model: str = "gpt-6-luna"  # grouping passes
    verify_model: str = "gpt-6-luna"
    batch_size: int = 60
    overlap: float = 0.5
    verify_batch: int = 12
    distractors: int = 3
    verify_repeats: int = 2
    min_votes: int = 2
    prompt: Prompt = field(default_factory=default_prompt)

    @property
    def ref(self) -> ComponentRef:
        return ComponentRef.of("resolver", "name_only_plan", "1", {
            "model": self.model, "verify_model": self.verify_model,
            "batch_size": self.batch_size, "overlap": self.overlap,
            "verify_batch": self.verify_batch, "distractors": self.distractors,
            "verify_repeats": self.verify_repeats, "min_votes": self.min_votes,
            "prompt": self.prompt.ref.ref})  # fmt: skip


def collapse_key(view: dict[str, list[str]]) -> str | None:
    """The organization name key with its legal form: "Premier Services GmbH" and
    "Premier Services LLC" stay apart for the model to judge, since collapsing is
    deterministic and nothing can undo it within the run (design 01 §12, question 2).
    The key is the cleaned name's (design 08 §3), so "Amazon, Inc. - 4411" and "SQ *AMAZON
    INC" collapse with "Amazon Inc" while "Intercede 2296 Ltd" and "Intercede 2302 Ltd",
    numbered companies, stay apart."""
    if view.get("name.truncated"):
        return None  # a descriptor cut short may be any of several names: its own node
    tokens = list(view.get("name.tokens", ()))
    legal = list(view.get("legal_form", ())[:1])
    stripped = []
    while len(tokens) > 1 and tokens[-1] in LEGAL_FORMS:
        stripped.append(LEGAL_FORMS[tokens.pop()])
    if stripped and not legal:
        legal = [" ".join(reversed(stripped))]
    keys = view.get("name.key")
    base = "".join(tokens) if tokens else (keys[0] if keys else None)
    if base is None:
        return None
    return f"{base}|{legal[0]}" if legal else base


def _sort_key(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", text.casefold()).strip()


def windows(order: Sequence[str], size: int, overlap: float) -> list[list[str]]:
    """Consecutive windows of ``size`` over ``order``, each starting ``size * (1 -
    overlap)`` after the last; the final window reaches the end."""
    if len(order) <= size:
        return [list(order)] if len(order) > 1 else []
    step = max(1, int(size * (1 - overlap)))
    starts = list(range(0, len(order) - size + 1, step))
    if starts[-1] + size < len(order):
        starts.append(len(order) - size)
    return [list(order[s : s + size]) for s in starts]


# -- votes -------------------------------------------------------------------------------------


@dataclass
class PairStats:
    raw: dict[str, bool] = field(default_factory=dict)  # stage -> co-assigned
    derived: dict[str, bool] = field(default_factory=dict)
    echoes: int = 0
    label_convergence: bool = False

    @property
    def n(self) -> int:
        return len(self.raw)

    @property
    def k(self) -> int:
        return sum(self.raw.values())

    @property
    def positive(self) -> bool:
        return self.k > 0 or any(self.derived.values()) or self.label_convergence


def pair_stats(proposals: Sequence[GroupProposal], views: dict[str, str]) -> dict:
    """Votes per node pair from the proposals of every stage (design 01 §5)."""
    per_stage: dict[tuple, list[bool]] = defaultdict(list)
    echoes: Counter = Counter()
    labels: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)  # stage -> node
    for p in proposals:
        view = views[p.stage_id]
        group = {m.node_id: (m.group_no if m.group_no is not None else f"single:{m.node_id}")
                 for m in p.members}  # fmt: skip
        shown = {m.node_id: m.shown for m in p.members}
        for m in p.members:
            if m.label_out and view == "raw":
                labels[p.stage_id].setdefault(m.node_id, (p.batch_id, m.label_out))
        for a, b in combinations(sorted(group), 2):
            if view == "derived" and shown[a] == shown[b]:
                echoes[(a, b)] += 1
                continue
            per_stage[((a, b), p.stage_id)].append(group[a] == group[b])
    stats: dict[tuple[str, str], PairStats] = defaultdict(PairStats)
    for (pair, stage), verdicts in per_stage.items():
        bucket = stats[pair].raw if views[stage] == "raw" else stats[pair].derived
        bucket[stage] = all(verdicts)
    for pair, n in echoes.items():
        stats[pair].echoes = n
    for by_node in labels.values():
        by_label: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for node, (batch, label) in by_node.items():
            by_label[_sort_key(label)].append((node, batch))
        for items in by_label.values():
            for (a, ba), (b, bb) in combinations(sorted(items), 2):
                if ba != bb:
                    stats[(a, b)].label_convergence = True
    return dict(stats)


def classify(s: PairStats | None, min_votes: int) -> str:
    """``VoteEdgePolicy`` (design 01 §6.2)."""
    if s is None:
        return "none"
    if s.n >= min_votes and s.k == s.n:
        return "strong"
    if s.n >= min_votes and s.k / s.n < 0.5:
        return "negative"
    if s.n >= min_votes:
        return "contested"
    return "unverified" if s.positive else "none"


# -- the run -------------------------------------------------------------------------------


def resolve(engine: Thisness, profile, scope: list[str], plan: NameOnlyPlan, *,
            commit: bool, actor: Actor) -> RunResult:  # fmt: skip
    from thisness.engine import RunResult

    plan = replace(plan, prompt=plan.prompt.for_profile(profile))
    key_of = profile.collapse_key or collapse_key
    store, now, ids = engine.store, engine.clock(), engine.ids
    run = engine._new_run(profile, scope, purpose="resolve", plan="name_only",
                          plan_ref=plan.ref.ref)  # fmt: skip
    store.register_component(plan.ref, {"model": plan.model})
    store.register_component(plan.prompt.ref, {"name": plan.prompt.name,
                                               "version": plan.prompt.version})  # fmt: skip
    scope_set = set(scope)

    # 1. Collapse, and match collapse keys against existing entities.
    features = store.features(scope)
    observations = {o.observation_id: o for o in store.observations(scope)}
    groups: dict[str, list[str]] = defaultdict(list)
    for obs in scope:
        key = key_of(features.get(obs, {}))
        groups[key or f"#{obs}"].append(obs)
    stored = [o for o in store.observation_ids(profile.entity_type) if o not in scope_set]
    entity_of = store.entities_of(stored)
    entity_by_key: dict[str, set[str]] = defaultdict(set)
    for obs, view in store.features(sorted(entity_of)).items():
        if (key := key_of(view)) in groups:
            entity_by_key[key].add(entity_of[obs])
    entity_nodes = sorted({e for es in entity_by_key.values() for e in es})
    members = store.members_of(entity_nodes)
    collapsed: dict[str, list[str]] = {}
    shown: dict[str, str] = {}
    for key, obs_ids in sorted(groups.items()):
        rep = sorted(obs_ids)[0]
        collapsed[rep] = sorted(obs_ids)
        shown[rep] = str((observations[rep].attributes or {}).get("name", key))
    for entity in entity_nodes:
        snapshot = engine.get_entity(entity)[1]
        shown[entity] = str(snapshot.attributes.get("name", entity)) if snapshot else entity
    nodes = sorted(shown)
    # Windows follow the cleaned name where there is one, so "SQ *HOWDEN JOINERY" sits
    # beside "Howden Joinery" rather than among the S's (design 08 §13.3).
    keys = {n: (features.get(n, {}).get("name.key") or [None])[0] for n in nodes}
    order = sorted(nodes, key=lambda n: (keys[n] or _sort_key(shown[n]), n))

    # 2-4. Stages.
    proposals: list[GroupProposal] = []
    views: dict[str, str] = {}

    def stage(kind: str, view: str, model: str, batches: list[list[str]],
              display: dict[str, str], conditioned_on: tuple[str, ...] = ()) -> str:  # fmt: skip
        stage_id = engine._stage(run.run_id, kind, plan.prompt.ref.ref, f"{model}:{plan.prompt.ref.ref}",
                                 now)  # fmt: skip
        views[stage_id] = view

        def one(batch: list[str], i: int) -> GroupProposal | None:
            return _group(engine, plan, run.run_id, stage_id, f"{kind}-{i}", model, batch,
                          display, profile.entity_type)  # fmt: skip

        with ThreadPoolExecutor(max_workers=max(1, plan.client.policy.concurrency)) as pool:
            done = list(pool.map(one, batches, range(len(batches))))
        proposals.extend(p for p in done if p is not None)
        return stage_id

    raw_stage = stage("group_proposer", "raw", plan.model,
                      windows(order, plan.batch_size, plan.overlap), shown)  # fmt: skip
    label_of: dict[str, str] = {}
    for p in proposals:
        for m in p.members:
            label_of.setdefault(m.node_id, m.label_out or m.shown)
    chained = {n: label_of.get(n, shown[n]) for n in nodes}
    chained_order = sorted(nodes, key=lambda n: (_sort_key(chained[n]), n))
    stage("group_proposer", "derived", plan.model,
          windows(chained_order, plan.batch_size, plan.overlap), chained, (raw_stage,))  # fmt: skip

    stats = pair_stats(proposals, views)
    position = {n: i for i, n in enumerate(order)}
    adjacency: dict[str, set[str]] = defaultdict(set)
    for (a, b), s in stats.items():
        if s.positive and classify(s, plan.min_votes) not in ("negative",):
            adjacency[a].add(b)
            adjacency[b].add(a)
    components = _components(nodes, adjacency)
    for repeat in range(plan.verify_repeats):
        batches = []
        for comp in components:
            batches += _verify_batches(comp, adjacency, order, position, plan, repeat)
        stage("verify", "raw", plan.verify_model, batches, shown)
    stats = pair_stats(proposals, views)

    # Edges, consensus and the plan, as for any run.
    held = engine._held_nodes()
    edges: dict[tuple[str, str], str] = {}
    for pair, s in stats.items():
        cls = classify(s, plan.min_votes)
        if cls == "none":
            continue
        if cls == "strong" and (pair[0] in held or pair[1] in held):
            cls = "contested"
        edges[pair] = cls
    for key, entities in entity_by_key.items():
        rep = sorted(groups[key])[0]
        for entity in entities:
            edges[(rep, entity) if rep < entity else (entity, rep)] = "strong"
    for constraint, left, right in store.constraints_among(entity_nodes):
        if left != right:
            pair = (left, right) if left < right else (right, left)
            edges[pair] = "cannot_link" if constraint.kind == "cannot_link" else "strong"
    states = store.entity_states(entity_nodes)
    result = engine.consensus.run(nodes, edges, states)
    # A name no successful batch ever showed was compared with nothing: it gets an entity,
    # but a provisional one, so later evidence can merge it without review (design 01 D5).
    shown_ok = {m.node_id for p in proposals for m in p.members}
    result = replace(result, clusters=tuple(
        replace(c, outcome=ConsensusOutcome.CREATE_PROVISIONAL)
        if c.outcome == ConsensusOutcome.CREATE_ENTITY and not set(c.members) <= shown_ok else c
        for c in result.clusters))  # fmt: skip

    vote_stage = engine._stage(run.run_id, "pair_resolver", plan.ref.ref, "votes", now)
    judgments, evidence, strong = [], [], {}
    for pair, cls in sorted(edges.items()):
        s = stats.get(pair)
        item = EvidenceItem(
            evidence_id=ids.evidence(), left_id=pair[0], right_id=pair[1],
            kind="llm_cluster_stability" if s else "normalized_exact",
            attribute="llm_votes" if s else "collapse_key", level=cls,
            direction="supports" if cls == "strong" else
            "contradicts" if cls in ("negative", "cannot_link") else "neutral",
            value={"agreeing": s.k, "votes": s.n, "derived": len(s.derived),
                   "echoes": s.echoes, "label_convergence": s.label_convergence} if s else None,
            method_ref=plan.ref.ref, run_id=run.run_id, stage_id=vote_stage, created_at=now,
        )  # fmt: skip
        evidence.append(item)
        judgment = PairJudgment(
            decision_id=ids.decision(), actor=Actor(kind="agent", id=f"votes:{plan.model}"),
            run_id=run.run_id, stage_id=vote_stage, left_id=pair[0], right_id=pair[1],
            verdict=VERDICTS[cls],
            scores=(Score(kind="llm_votes", value=s.n if s else 0),
                    Score(kind="llm_coassignment_rate", value=s.k / s.n if s and s.n else 0.0)),
            evidence_ids=(item.evidence_id,), component_refs=(plan.ref.ref,), created_at=now,
        )  # fmt: skip
        judgments.append(judgment)
        if cls == "strong":
            strong[pair] = judgment.decision_id

    expanded = ConsensusResult(
        clusters=tuple(replace(
            c, members=tuple(sorted(m for n in c.members for m in collapsed.get(n, [n]))),
            linked=tuple(sorted(m for n in c.linked for m in collapsed.get(n, [n]))))
            for c in result.clusters),
        boundaries=result.boundaries,
    )  # fmt: skip
    refs = (engine.consensus.ref.ref, plan.ref.ref, engine.policy.ref.ref, profile.ref.ref)
    ops, outcomes, decisions, reviews = engine._plan(
        profile, None, run, scope_set, expanded, {}, {}, members, states, now, strong, refs=refs
    )
    store.record_proposals(proposals)
    store.record_evidence(evidence)
    store.record_decisions([*judgments, *decisions])
    changes = ChangeSet(run_id=run.run_id, base_seq=run.base_seq, ops=tuple(ops),
                        read_set=ReadSet(entity_ids=frozenset(entity_nodes),
                                         blocking_keys=frozenset(f"collapse={k}" for k in groups)))  # fmt: skip
    client = plan.client
    run_stats: dict[str, Any] = {
        "scope": len(scope),
        "plan": "name_only",
        "nodes": len(nodes),
        "collapsed_groups": sum(1 for g in groups.values() if len(g) > 1),
        "entities_matched": len(entity_nodes),
        "proposals": len(proposals),
        "never_shown": len(set(nodes) - shown_ok),
        "verify_components": len(components),
        "edges": dict(Counter(edges.values())),
        "clusters": len(result.clusters),
        "calls": client.calls,
        "spent_usd": round(client.spent_usd, 6),
    }
    if not commit:
        store.close_run(run.run_id, "proposed")
        return RunResult(run.run_id, False, outcomes, None, changes, reviews, (), run_stats)
    committed = store.commit(changes, actor=actor, canonicalizer=engine.canonicalizer)
    return RunResult(run.run_id, True, outcomes, committed, changes, reviews, (), run_stats)


def _group(engine, plan, run_id, stage_id, batch_id, model, batch, display, entity_type):
    """One batch to the model, as a proposal; None when there is no usable answer."""
    refs = {n: f"N{i + 1}" for i, n in enumerate(batch)}
    payload = {"entity_type": entity_type,
               "items": [{"ref": refs[n], "name": display[n], "record_id": n} for n in batch]}  # fmt: skip
    try:
        answer = plan.client.call(plan.prompt, model=model, payload=payload, run_id=run_id,
                                  stage_id=stage_id)  # fmt: skip
    except BudgetExhausted:
        return None
    if not answer.ok:
        return None
    seen: set[str] = set()
    for group in answer.parsed.groups:
        if not set(group.members) <= set(refs.values()) or seen & set(group.members):
            return None  # an unknown ref or an item in two groups: no usable answer
        seen |= set(group.members)
    group_of = {r: (i, g) for i, g in enumerate(answer.parsed.groups) for r in g.members}
    members = []
    for n in batch:
        i, g = group_of.get(refs[n], (None, None))
        members.append(ProposalMember(node_id=n, shown=display[n], group_no=i,
                                      label_out=g.label if g else None,
                                      uncertain=bool(g) and refs[n] in g.uncertain))  # fmt: skip
    return GroupProposal(proposal_id=engine.ids.proposal(), run_id=run_id, stage_id=stage_id,
                         batch_id=batch_id, members=tuple(members),
                         invocation_id=answer.invocation.invocation_id)  # fmt: skip


def _components(nodes: Sequence[str], adjacency: dict[str, set[str]]) -> list[list[str]]:
    seen: set[str] = set()
    out = []
    for start in sorted(nodes):
        if start in seen or not adjacency.get(start):
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            node = stack.pop()
            comp.append(node)
            for other in sorted(adjacency[node] - seen):
                seen.add(other)
                stack.append(other)
        if len(comp) > 1:
            out.append(sorted(comp))
    return out


def _verify_batches(comp, adjacency, order, position, plan, repeat) -> list[list[str]]:
    """A component shown with raw names and nearby distractors; a large one in
    sub-batches that share its two best-connected members (design 01 §6.4)."""
    inside = set(comp)
    near: list[tuple[int, str]] = []
    for node in comp:
        p = position[node]
        for step in range(1, 4 * plan.distractors + 2):
            for q in (p - step, p + step):
                if 0 <= q < len(order) and order[q] not in inside:
                    near.append((step, order[q]))
    candidates = list(dict.fromkeys(n for _, n in sorted(near)))
    distractors = candidates[repeat * plan.distractors : (repeat + 1) * plan.distractors]
    room = max(2, plan.verify_batch - len(distractors))
    if len(comp) <= room:
        return [sorted(comp + distractors)]
    anchors = sorted(comp, key=lambda n: (-len(adjacency[n] & inside), n))[:2]
    rest = [n for n in comp if n not in anchors]
    size = max(1, room - 2)
    return [sorted(anchors + rest[i : i + size] + distractors) for i in range(0, len(rest), size)]
