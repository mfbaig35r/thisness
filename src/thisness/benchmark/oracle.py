"""A simulated model that knows the benchmark's ground truth (design 05 §7). It answers
the adjudicator's pair questions and the review assistant's review questions.

It answers each pair from the truth and then errs in controlled ways, so the
pipeline and its policy can be measured at scale without spending: how many false
merges a model that is wrong 5% of the time causes in ``decide`` mode, or one that
folds related companies into each other 20% of the time. It reads only what a real
model would be sent, plus the record refs, and is deterministic for a seed.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Callable, Mapping
from itertools import combinations

from thisness.llm.providers import FunctionProvider
from thisness.llm.types import ModelRequest


def oracle_provider(
    truth_of: Callable[[str], str | None],
    related: Callable[[str, str], bool],
    *,
    error: float = 0.0,
    unsure: float = 0.0,
    fold_related: float = 0.0,
    seed: int = 0,
    local: bool = True,
) -> FunctionProvider:
    """``error``: answer the opposite of the truth (same for different and back).
    ``unsure``: decline to answer. ``fold_related``: call related entities the same."""

    def review(payload: dict) -> dict:
        """A review recommendation: the records grouped by their true entity."""
        relations = payload.get("relations") or ["related_to"]
        records = payload["records"]
        rng = random.Random(f"{seed}|{records[0]['record_id']}|review")
        by_truth: dict[str | None, list[str]] = {}
        for r in records:
            truth = truth_of(r["record_id"])
            by_truth.setdefault(truth if truth is not None else r["ref"], []).append(r["ref"])
        groups = sorted(sorted(g) for g in by_truth.values())
        current = sorted(sorted(g) for g in payload["current_groups"])
        truths = [t for t in by_truth if t is not None]
        x = rng.random()
        if x < unsure:
            return {"resolution": "unsure", "rationale": "simulated"}
        if x < unsure + error:
            groups = [sorted(r["ref"] for r in records)] if len(groups) > 1 else \
                [[r["ref"]] for r in records]  # fmt: skip
        elif len(truths) == 2 and related(*truths) and rng.random() < fold_related:
            groups = [sorted(r["ref"] for r in records)]
        cited = [c["id"] for c in payload["comparisons"] if (c["weight"] or 0) > 0]
        if len(groups) == 1:
            out = {"resolution": "same"}
        elif len(truths) == 2 and groups == current and related(*truths):
            out = {"resolution": "related", "relation": relations[0]}
        elif groups == current:
            out = {"resolution": "different"}
        else:
            out = {"resolution": "groups", "groups": groups}
        return {**out, "cited": cited, "reason_codes": ["OTHER"], "rationale": "simulated"}

    def grouping(payload: dict) -> dict:
        """A name-only grouping: items grouped by true entity, each labeled with it, so
        separate batches converge on one label for one entity (design 01 §5.3)."""
        items = payload["items"]
        rng = random.Random(f"{seed}|{items[0]['record_id']}|{len(items)}|group")
        truth = {it["ref"]: truth_of(it["record_id"]) or it["ref"] for it in items}
        if fold_related:
            # A model that folds related entities together: one label per related set.
            keys = sorted(set(truth.values()))
            merged = {k: k for k in keys}
            for a, b in combinations(keys, 2):
                if related(a, b) and random.Random(f"{seed}|{a}|{b}|fold").random() < fold_related:
                    merged[b] = merged[a]
            truth = {ref: merged[t] for ref, t in truth.items()}
        by_truth: dict[str, list[str]] = {}
        for ref, t in truth.items():
            x = rng.random()
            if x < unsure:
                continue
            if x < unsure + error:
                t = rng.choice(sorted(set(truth.values())))  # a wrong group, or the right one
            by_truth.setdefault(t, []).append(ref)
        return {"groups": [{"members": sorted(refs), "label": str(t)}
                           for t, refs in sorted(by_truth.items())]}  # fmt: skip

    def answer(request: ModelRequest) -> dict:
        payload = json.loads(request.messages[0].content)
        if "items" in payload:
            return grouping(payload)
        if "records" in payload:
            return review(payload)
        relations = payload.get("relations") or ["related_to"]
        out = []
        for p in payload["pairs"]:
            a, b = p["a"]["ref"], p["b"]["ref"]
            ta, tb = truth_of(a), truth_of(b)
            rng = random.Random(f"{seed}|{a}|{b}")
            if ta is not None and ta == tb:
                correct = "same"
            elif ta is not None and tb is not None and related(ta, tb):
                correct = "related"
            else:
                correct = "different"
            x = rng.random()
            if x < unsure:
                decision = "unsure"
            elif x < unsure + error:
                decision = "different" if correct == "same" else "same"
            elif correct == "related" and rng.random() < fold_related:
                decision = "same"
            else:
                decision = correct
            positive = [c["id"] for c in p["comparisons"] if c["bits"] > 0]
            negative = [c["id"] for c in p["comparisons"] if c["bits"] < 0]
            out.append({
                "pair": p["pair"], "decision": decision,
                "relation": relations[0] if decision == "related" else None,
                "supporting": positive if decision == "same" else negative,
                "contradicting": negative if decision == "same" else positive,
                "reason_codes": ["OTHER"], "rationale": "simulated",
            })  # fmt: skip
        return {"answers": out}

    return FunctionProvider(answer, name="oracle", local=local)


def benchmark_truth(engine, truth: Mapping[str, str]) -> Callable[[str], str | None]:
    """A record's true entity; an entity's is its members' majority truth."""

    def truth_of(node: str) -> str | None:
        if node in truth:
            return truth[node]
        members = [truth[m] for m in engine.members(node) if m in truth]
        return Counter(members).most_common(1)[0][0] if members else None

    return truth_of
