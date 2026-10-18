"""The LLM adjudicator (design 05 §4, PRD appendix A.7).

A model judges the node pairs a run's Fellegi-Sunter edge policy left contested, as
one more pair resolver in its own family. It is given each pair's two records,
filtered by the profile's privacy policy, and the comparison results behind the
pair, each with an id it must cite. Answers that cite ids they were not given, or
name a relation the profile does not offer, are discarded.

In ``shadow`` mode answers are recorded and change nothing. In ``decide`` mode a
``same`` answer makes the edge strong when the Fellegi-Sunter weight is at least
``floor_bits``, and a ``different`` answer makes it negative; consensus, holdout
and the blast-radius policy then apply exactly as they do to any edge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import BaseModel, Field

from thisness.core.components import ComponentRef
from thisness.llm.client import Answer, BudgetExhausted, LLMClient
from thisness.llm.privacy import filter_attributes
from thisness.llm.prompts import Prompt

Decision = Literal["same", "different", "related", "unsure"]
ReasonCode = Literal[
    "NAME_EQUIVALENT", "NAME_TYPO", "ABBREVIATION_OR_ACRONYM", "LEGAL_FORM_ONLY",
    "NICKNAME_OR_TRANSLITERATION", "SHARED_IDENTIFIER", "CONFLICTING_IDENTIFIER",
    "SHARED_CONTACT", "SAME_LOCATION", "DIFFERENT_LOCATION", "DIFFERENT_DESCRIPTOR",
    "COMMON_NAME", "SHARED_HOUSEHOLD", "DIFFERENT_BIRTH_DATE", "GROUP_OR_PARENT",
    "INSUFFICIENT_EVIDENCE", "OTHER",
]  # fmt: skip


class PairAnswer(BaseModel):
    pair: str
    decision: Decision
    relation: str | None = None
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    rationale: str | None = Field(default=None, max_length=600)


class AdjudicationAnswer(BaseModel):
    answers: list[PairAnswer]


def default_prompt() -> Prompt:
    return Prompt.load("adjudicate_pairs", "2", AdjudicationAnswer)


@dataclass(frozen=True)
class PairCase:
    """One contested node pair, as the adjudicator sees it."""

    nodes: tuple[str, str]
    records: tuple[dict[str, Any], dict[str, Any]]  # unfiltered; filtered on the way out
    comparisons: tuple[dict[str, Any], ...]  # {"id", "comparison", "level", "bits"}
    evidence_keys: Mapping[str, tuple[str, str, str]]  # local id -> (left, right, comparison)
    weight: float  # the strongest Fellegi-Sunter weight behind the pair


@dataclass(frozen=True)
class Adjudication:
    case: PairCase
    answer: PairAnswer | None
    invocation_id: str | None
    status: str  # ok, invalid_citation, unknown_relation, missing, failed, skipped
    applied: bool = False


@dataclass
class Adjudicator:
    client: LLMClient
    model: str = "gpt-6-luna"
    mode: Literal["shadow", "decide"] = "shadow"
    floor_bits: float = 6.0
    pairs_per_call: int = 8
    max_pairs: int | None = 500
    prompt: Prompt = field(default_factory=default_prompt)
    # A model's "same" yields to a contradiction (design 06 §10.7): where the edges it
    # set strong would put a negative or cannot-link pair inside one component, they go
    # back to contested and the component is settled on the evidence alone. A wrong
    # "same" then costs a review of that pair, not a hold on a whole taxon.
    same_yields: bool = True

    @property
    def ref(self) -> ComponentRef:
        config = {"model": self.model, "mode": self.mode, "floor_bits": self.floor_bits,
                  "pairs_per_call": self.pairs_per_call, "prompt": self.prompt.ref.ref}  # fmt: skip
        if not self.same_yields:  # the original policy keeps its original ref
            return ComponentRef.of("resolver", "llm_adjudicator", "1", config)
        return ComponentRef.of("resolver", "llm_adjudicator", "2", {**config, "same_yields": True})

    def edge(self, adjudication: Adjudication) -> str | None:
        """The edge class a decided answer sets, or None to leave the edge as it is."""
        answer = adjudication.answer
        if self.mode != "decide" or answer is None:
            return None
        if answer.decision == "same" and adjudication.case.weight >= self.floor_bits:
            return "strong"
        if answer.decision == "different":
            return "negative"
        return None

    def for_profile(self, profile: Any) -> Adjudicator:
        """This adjudicator with the profile's guidance in its prompt (design 06 §6)."""
        prompt = self.prompt.for_profile(profile)
        return self if prompt is self.prompt else replace(self, prompt=prompt)

    def adjudicate(
        self,
        cases: Sequence[PairCase],
        *,
        entity_type: str,
        attributes: Mapping[str, Any],
        relations: Sequence[str],
        run_id: str | None = None,
        stage_id: str | None = None,
    ) -> list[Adjudication]:
        ordered = sorted(cases, key=lambda c: (-c.weight, c.nodes))
        chosen = ordered if self.max_pairs is None else ordered[: self.max_pairs]
        skipped = [Adjudication(c, None, None, "skipped") for c in ordered[len(chosen) :]]
        batches = [chosen[i : i + self.pairs_per_call]
                   for i in range(0, len(chosen), self.pairs_per_call)]  # fmt: skip
        local = self.client.provider.local

        def payload(batch: Sequence[PairCase]) -> dict[str, Any]:
            pairs = []
            for i, case in enumerate(batch):
                a, b = (dict(zip(("fields", "withheld"),
                                 filter_attributes(r, attributes, local=local), strict=True))
                        for r in case.records)  # fmt: skip
                pairs.append({"pair": f"P{i + 1}", "a": {"ref": case.nodes[0], **a},
                              "b": {"ref": case.nodes[1], **b},
                              "comparisons": list(case.comparisons)})  # fmt: skip
            return {"entity_type": entity_type, "relations": list(relations), "pairs": pairs}

        def run(batch: Sequence[PairCase]) -> list[Adjudication]:
            try:
                result: Answer = self.client.call(self.prompt, model=self.model,
                                                  payload=payload(batch), run_id=run_id,
                                                  stage_id=stage_id)  # fmt: skip
            except BudgetExhausted:
                return [Adjudication(c, None, None, "skipped") for c in batch]
            invocation = result.invocation.invocation_id
            if not result.ok:
                return [Adjudication(c, None, invocation, "failed") for c in batch]
            by_slot = {}
            for answer in result.parsed.answers:
                by_slot.setdefault(answer.pair, answer)
            out = []
            for i, case in enumerate(batch):
                answer = by_slot.get(f"P{i + 1}")
                if answer is None:
                    out.append(Adjudication(case, None, invocation, "missing"))
                elif not set(answer.supporting + answer.contradicting) <= set(case.evidence_keys):
                    out.append(Adjudication(case, None, invocation, "invalid_citation"))
                elif answer.decision == "related" and answer.relation not in relations:
                    out.append(Adjudication(case, None, invocation, "unknown_relation"))
                else:
                    out.append(Adjudication(case, answer, invocation, "ok"))
            return out

        workers = max(1, self.client.policy.concurrency)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = [a for batch in pool.map(run, batches) for a in batch]
        return results + skipped
