"""Review items (design 02 §6.3). The queue UX is M2; M1 persists them."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from thisness.core.base import Model
from thisness.core.types import DecisionId, LedgerSeq, NodeId, ReviewId, RunId

ReviewKind = Literal["cluster", "boundary", "merge", "split"]
ReviewStatus = Literal["open", "resolved", "dismissed"]


class ReviewItem(Model):
    review_id: ReviewId
    kind: ReviewKind
    run_id: RunId | None = None
    decision_id: DecisionId  # the decision that opened it
    members: tuple[NodeId, ...] = Field(min_length=1)  # under an auto-merge hold while open
    status: ReviewStatus
    opened_seq: LedgerSeq
    closed_seq: LedgerSeq | None = None
    resolution_decision_id: DecisionId | None = None

    @model_validator(mode="after")
    def _closure_is_consistent(self) -> Self:
        closed = self.status != "open"
        if closed != (self.closed_seq is not None):
            raise ValueError("closed_seq is set exactly when the review is closed")
        if self.status == "resolved" and self.resolution_decision_id is None:
            raise ValueError("a resolved review cites the decision that resolved it")
        return self
