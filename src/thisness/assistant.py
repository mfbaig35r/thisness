"""The review assistant (design 05 §5, PRD appendix A.15).

A model answers an open review's own question from its case, and the answer is
recorded as a ``ReviewRecommendation``: shown to the reviewer as a suggestion that
can be applied to the board, never submitted, never a label. When the reviewer
answers, their ``ReviewDecision`` records which recommendation they saw, so the
model's agreement with people is measured on real decisions.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import combinations
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from thisness.adjudicate import ReasonCode
from thisness.core.components import ComponentRef
from thisness.core.decisions import Actor, ReviewDecision, ReviewRecommendation
from thisness.llm.client import BudgetExhausted, LLMClient
from thisness.llm.privacy import filter_attributes
from thisness.llm.prompts import Prompt
from thisness.profiles.base import AttributeDefinition

if TYPE_CHECKING:
    from thisness.engine import Thisness

MAX_COMPARISONS = 40


class RecommendationAnswer(BaseModel):
    resolution: Literal["same", "different", "groups", "related", "unsure"]
    groups: list[list[str]] | None = None
    relation: str | None = None
    cited: list[str] = Field(default_factory=list)
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    rationale: str | None = Field(default=None, max_length=800)


def default_prompt() -> Prompt:
    return Prompt.load("recommend_review", "2", RecommendationAnswer)


@dataclass(frozen=True)
class Recommended:
    review_id: str
    recommendation: ReviewRecommendation | None
    invocation_id: str | None
    status: str  # ok, invalid_groups, invalid_citation, unknown_relation, failed, skipped


def review_labels(engine: Thisness, case: Any) -> tuple[dict[str, str], list[tuple]]:
    """The labels a review's records and comparisons carry in the assistant's request:
    records R1, R2, ... in the case's order; comparisons C1, C2, ... strongest first,
    as ``(label, judgment, record label, record label, weight)``. The Workbench shows the
    same labels, so a suggestion's rationale reads against the board."""
    refs = {node.node_id: f"R{i + 1}" for i, node in enumerate(case.nodes)}
    member_of = {member: node.node_id for node in case.nodes if node.kind == "entity"
                 for member in engine.members(node.node_id)}  # fmt: skip

    def ref_of(obs: str) -> str | None:
        return refs.get(obs) or refs.get(member_of.get(obs, ""))

    scored = []
    for j in case.judgments:
        a, b = ref_of(j.left_id), ref_of(j.right_id)
        if a is None or b is None or a == b:
            continue
        weight = next((float(s.value) for s in j.scores
                       if s.kind == "fellegi_sunter_weight"), None)  # fmt: skip
        scored.append((abs(weight) if weight is not None else 0.0, j, a, b, weight))
    ranked = sorted(scored, key=lambda x: (-x[0], x[1].decision_id))[:MAX_COMPARISONS]
    return refs, [(f"C{i + 1}", j, a, b, w) for i, (_, j, a, b, w) in enumerate(ranked)]


@dataclass(frozen=True)
class _Request:
    review_id: str
    prompt: Prompt
    payload: dict[str, Any]
    refs: dict[str, str]
    node_of_ref: dict[str, str]
    current: dict[str, list[str]]
    judgment_of: dict[str, str]
    relations: list[str]


@dataclass
class ReviewAssistant:
    client: LLMClient
    model: str = "claude-sonnet-5"
    prompt: Prompt = field(default_factory=default_prompt)

    @property
    def ref(self) -> ComponentRef:
        return ComponentRef.of("resolver", "review_assistant", "1",
                               {"model": self.model, "prompt": self.prompt.ref.ref})  # fmt: skip

    def _prepare(self, engine: Thisness, review_id: str) -> _Request:
        """Everything the request needs, read from the store."""
        case = engine.review_case(review_id)
        refs, labelled = review_labels(engine, case)
        node_of_ref = {r: n for n, r in refs.items()}
        entity_type = None
        current: dict[str, list[str]] = {}
        records = []
        local = self.client.provider.local
        for node in case.nodes:
            current.setdefault(node.entity_id or node.node_id, []).append(refs[node.node_id])
            profile = engine.profiles.get(engine.get_entity(node.entity_id)[0].entity_type) \
                if node.entity_id else None  # fmt: skip
            entity_type = entity_type or (profile.entity_type if profile else None)
            definitions = dict(profile.attributes) if profile else {}
            attributes = dict(node.attributes)
            if node.kind == "entity":
                attributes["observation_count"] = node.size
                definitions["observation_count"] = AttributeDefinition(
                    name="observation_count", data_type="integer")  # fmt: skip
            fields, withheld = filter_attributes(attributes, definitions, local=local)
            source = node.source.get("type") if node.source else None
            records.append({"ref": refs[node.node_id], "record_id": node.node_id,
                            "kind": node.kind, "source": source, "fields": fields,
                            "withheld": withheld})  # fmt: skip

        comparisons, judgment_of = [], {}
        evidence = {e.evidence_id: e for e in engine.store.evidence(
            sorted({e for _, j, *_ in labelled for e in j.evidence_ids}))}  # fmt: skip
        for local_id, j, a, b, weight in labelled:
            levels = {e.attribute: e.level for i in j.evidence_ids if (e := evidence.get(i))
                      and e.attribute and e.level}  # fmt: skip
            comparisons.append({"id": local_id, "a": a, "b": b, "by": j.actor.kind,
                                "weight": None if weight is None else round(weight, 1),
                                "verdict": str(j.verdict), "levels": levels})  # fmt: skip
            judgment_of[local_id] = j.decision_id

        profile = engine.profiles.get(entity_type) if entity_type else None
        relations = list(profile.relations) if profile else ["related_to"]
        payload: dict[str, Any] = {
            "entity_type": entity_type, "question": case.question, "relations": relations,
            "records": records, "current_groups": sorted(current.values()),
            "comparisons": comparisons,
        }  # fmt: skip
        prompt = self.prompt.for_profile(engine.profiles.get(entity_type) if entity_type else None)
        return _Request(review_id, prompt, payload, refs, node_of_ref, current, judgment_of,
                        relations)  # fmt: skip

    def _ask(self, request: _Request) -> Any:
        """The model call: the only step that runs in parallel."""
        try:
            return self.client.call(request.prompt, model=self.model, payload=request.payload)
        except BudgetExhausted:
            return None

    def _finish(self, engine: Thisness, request: _Request, answer: Any) -> Recommended:
        """Check the answer and record it, one review at a time."""
        review_id, prompt, refs = request.review_id, request.prompt, request.refs
        node_of_ref, current = request.node_of_ref, request.current
        judgment_of, relations = request.judgment_of, request.relations
        if answer is None:
            return Recommended(review_id, None, None, "skipped")
        invocation = answer.invocation.invocation_id
        if not answer.ok:
            return Recommended(review_id, None, invocation, "failed")
        parsed: RecommendationAnswer = answer.parsed
        if not set(parsed.cited) <= set(judgment_of):
            return Recommended(review_id, None, invocation, "invalid_citation")
        groups = _partition(parsed, sorted(current.values()), sorted(refs.values()))
        if parsed.resolution != "unsure" and groups is None:
            return Recommended(review_id, None, invocation, "invalid_groups")
        if parsed.resolution == "related" and parsed.relation not in relations:
            return Recommended(review_id, None, invocation, "unknown_relation")
        engine.store.register_component(self.ref, {"model": self.model})
        recommendation = ReviewRecommendation(
            decision_id=engine.ids.decision(), actor=Actor(kind="agent", id=self.model),
            review_id=review_id, resolution=parsed.resolution,
            groups=None if groups is None else tuple(
                tuple(sorted(node_of_ref[r] for r in g)) for g in groups),
            relation=parsed.relation if parsed.resolution == "related" else None,
            reason_codes=tuple(parsed.reason_codes), rationale=parsed.rationale,
            cited_judgments=tuple(judgment_of[c] for c in parsed.cited),
            component_refs=(self.ref.ref, prompt.ref.ref),
            invocation_id=invocation, created_at=engine.clock(),
        )  # fmt: skip
        engine.store.record_decisions([recommendation])
        return Recommended(review_id, recommendation, invocation, "ok")

    def recommend(self, engine: Thisness, review_id: str) -> Recommended:
        request = self._prepare(engine, review_id)
        return self._finish(engine, request, self._ask(request))

    def recommend_many(self, engine: Thisness, review_ids: Sequence[str]) -> list[Recommended]:
        """Recommendations for many reviews, the model asked ``policy.concurrency`` at a
        time. Reading the store and recording answers stay sequential and in order."""
        requests = [self._prepare(engine, r) for r in review_ids]
        workers = max(1, min(self.client.policy.concurrency, len(requests)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            answers = list(pool.map(self._ask, requests))
        return [self._finish(engine, r, a) for r, a in zip(requests, answers, strict=True)]


def _partition(answer: RecommendationAnswer, current: list[list[str]],
               refs: list[str]) -> list[list[str]] | None:  # fmt: skip
    """The grouping an answer means, or None when it is not a valid one."""
    if answer.resolution == "unsure":
        return None
    if answer.resolution == "same":
        return [refs]
    if answer.resolution == "different":
        return current
    if answer.resolution == "related":
        return current if len(current) == 2 else None
    groups = [list(g) for g in answer.groups or [] if g]
    flat = [r for g in groups for r in g]
    return groups if sorted(flat) == refs and len(flat) == len(set(flat)) else None


def agreement(decisions: Sequence[ReviewDecision],
              recommendations: dict[str, ReviewRecommendation]) -> dict[str, Any]:  # fmt: skip
    """How often reviewers' answers matched the recommendation they were shown: the
    whole grouping, and pair by pair (same group or not)."""
    exact = pairs = agreed = n = 0
    by_resolution: dict[str, list[int]] = {}
    for decision in decisions:
        rec = recommendations.get(decision.recommendation_id or "")
        if rec is None or rec.groups is None or decision.groups is None:
            continue
        n += 1
        mine = {node: i for i, g in enumerate(decision.groups) for node in g}
        theirs = {node: i for i, g in enumerate(rec.groups) for node in g}
        same_grouping = {frozenset(g) for g in decision.groups} == {frozenset(g)
                                                                    for g in rec.groups}  # fmt: skip
        exact += same_grouping
        by_resolution.setdefault(rec.resolution, []).append(int(same_grouping))
        for a, b in combinations(sorted(mine), 2):
            if a in theirs and b in theirs:
                pairs += 1
                agreed += (mine[a] == mine[b]) == (theirs[a] == theirs[b])
    return {
        "reviews": n,
        "exact": exact / n if n else None,
        "pairwise": agreed / pairs if pairs else None,
        "exact_by_recommendation": {k: sum(v) / len(v) for k, v in sorted(by_resolution.items())},
    }
