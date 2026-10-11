"""Labels from review decisions (design 04 §2.7).

Every answered review is ground truth about pairs of observations: two observations
the reviewer put in one group are a match, in different groups a non-match. Labels
are derived from stored decisions on demand, for calibrating models later (PRD V1).
M2 exports them; it does not retrain.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import TYPE_CHECKING, Any, Literal

from thisness.core.decisions import ReviewDecision

if TYPE_CHECKING:
    from thisness.engine import Thisness


@dataclass(frozen=True)
class Label:
    review_id: str
    decision_id: str
    left_id: str
    right_id: str
    label: Literal["match", "no_match"]
    relation: str | None  # set when the reviewer said the two sides are related
    judgment_id: str | None  # the latest resolver judgment on the pair, if it was scored
    model_ref: str | None
    scores: dict[str, Any]
    left_features: dict[str, list[str]]
    right_features: dict[str, list[str]]


def labels(engine: Thisness) -> list[Label]:
    """One label per observation pair a review answered: the pairs a resolver scored
    among the observations involved, plus every pair of observations the review named."""
    store = engine.store
    out: list[Label] = []
    for decision in store.decisions_of_type("review"):
        if not isinstance(decision, ReviewDecision) or decision.groups is None:
            continue  # dismissed: no answer
        # Entities in the review are read as they were when the answer was committed.
        events = list(store.ledger(decision_id=decision.decision_id))
        as_of = min(e.seq for e in events) - 1 if events else store.head_seq()
        group_of: dict[str, int] = {}
        named: list[str] = []
        for i, group in enumerate(decision.groups):
            for node in group:
                if node.startswith("obs_"):
                    group_of[node] = i
                    named.append(node)
        for i, group in enumerate(decision.groups):
            for node in group:
                if not node.startswith("obs_"):
                    for member in store.members(node, as_of_seq=as_of):
                        group_of.setdefault(member, i)
        latest = {}
        for judgment in store.pair_judgments(sorted(group_of)):
            key = (judgment.left_id, judgment.right_id)
            if key not in latest or judgment.decision_id > latest[key].decision_id:
                latest[key] = judgment
        pairs = set(latest) | {(a, b) for a, b in combinations(sorted(named), 2)}
        features = store.features(sorted({o for pair in pairs for o in pair}))
        for left, right in sorted(pairs):
            same = group_of[left] == group_of[right]
            judgment = latest.get((left, right))
            scores = {s.kind: s.value for s in judgment.scores} if judgment else {}
            model_ref = next((s.model_ref for s in judgment.scores if s.model_ref), None) \
                if judgment else None  # fmt: skip
            out.append(
                Label(
                    review_id=decision.review_id,
                    decision_id=decision.decision_id,
                    left_id=left,
                    right_id=right,
                    label="match" if same else "no_match",
                    relation=decision.relation if not same else None,
                    judgment_id=judgment.decision_id if judgment else None,
                    model_ref=model_ref,
                    scores=scores,
                    left_features=features.get(left, {}),
                    right_features=features.get(right, {}),
                )
            )
    return out
