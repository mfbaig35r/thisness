"""Resolution policy: what commits automatically (design 03 §12)."""

from __future__ import annotations

from dataclasses import dataclass

from thisness.core.components import ComponentRef
from thisness.core.decisions import ConsensusOutcome

AUTO = frozenset(
    {
        ConsensusOutcome.CREATE_ENTITY,
        ConsensusOutcome.CREATE_PROVISIONAL,
        ConsensusOutcome.ATTACH,
        ConsensusOutcome.MERGE_PROVISIONAL,
        ConsensusOutcome.NO_CHANGE,
    }
)


@dataclass(frozen=True)
class ResolutionPolicy:
    """Outcomes in ``AUTO`` commit; everything else opens a review.

    The blast-radius gate sends an attach to review when it would add more than
    ``max_attach`` observations to an entity in one commit, or more than
    ``growth_share`` of an entity that already has ``growth_min_size`` members. (A
    flat 50% rule would send every second observation of a new company to review.)"""

    max_attach: int = 25
    growth_share: float = 0.5
    growth_min_size: int = 10

    @property
    def ref(self) -> ComponentRef:
        return ComponentRef.of(
            "policy",
            "default",
            "1",
            {
                "auto": sorted(AUTO),
                "max_attach": self.max_attach,
                "growth_share": self.growth_share,
                "growth_min_size": self.growth_min_size,
            },
        )

    def auto_commits(self, outcome: ConsensusOutcome) -> bool:
        return outcome in AUTO

    def attach_needs_review(self, current_size: int, adding: int) -> bool:
        if adding > self.max_attach:
            return True
        return current_size >= self.growth_min_size and adding > self.growth_share * current_size
