"""Edge policies: from scored pairs to the edge classes consensus reads (design 03 §9)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from thisness.consensus import EdgeClass
from thisness.core.components import ComponentRef
from thisness.core.decisions import Verdict
from thisness.fellegi_sunter import FSModel

VERDICT = {
    "strong": Verdict.MATCH,
    "negative": Verdict.NO_MATCH,
    "cannot_link": Verdict.NO_MATCH,
    "contested": Verdict.POSSIBLE_MATCH,
    "unverified": Verdict.POSSIBLE_MATCH,
}
_RANK = {"strong": 3, "contested": 2, "unverified": 1, "negative": 0}


@dataclass(frozen=True)
class FellegiSunterEdgePolicy:
    """strong: a decisive rule matched, or weight >= T_high. negative: weight <= T_low.
    cannot_link: a constraint fired. contested: anything between.

    T_high defaults to the label-free threshold, the weight at which the posterior
    reaches ``posterior`` under the model's match rate (design 03 §11)."""

    posterior: float = 0.999
    t_low: float = 0.0
    t_high: float | None = None  # set from a benchmark; overrides ``posterior``

    @property
    def ref(self) -> ComponentRef:
        return ComponentRef.of(
            "edge_policy",
            "fellegi_sunter",
            "1",
            {"posterior": self.posterior, "t_low": self.t_low, "t_high": self.t_high},
        )

    def threshold(self, model: FSModel, floor: float | None = None) -> float:
        """An explicit ``t_high`` wins. Otherwise the label-free threshold, raised to
        ``floor`` (the profile's minimum strong weight) when it falls below it."""
        if self.t_high is not None:
            return self.t_high
        automatic = model.weight_for_posterior(self.posterior)
        return automatic if floor is None else max(automatic, floor)

    def classify(self, row: Mapping[str, Any], t_high: float,
                 t_low: float | None = None) -> EdgeClass:  # fmt: skip
        """``t_low`` overrides the policy's for one profile (``max_negative_weight``)."""
        if row["cannot_link"]:
            return "cannot_link"
        if row["rule_verdict"] == "MATCH" or row["weight"] >= t_high:
            return "strong"
        if row["weight"] <= (self.t_low if t_low is None else t_low):
            return "negative"
        return "contested"


def aggregate(
    rows: Iterable[tuple[str, str, EdgeClass]], node_of: Callable[[str], str | None]
) -> dict[tuple[str, str], tuple[EdgeClass, list[int]]]:
    """Fold observation-pair classes into node-pair classes (design 03 §9).

    An observation compared with several members of one entity gets one edge to that
    entity: its best support, but carrying any contradiction. Any cannot-link wins; a
    strong edge alongside a negative member becomes contested, so a fringe member
    cannot pull an observation into an entity whose other members contradict it.
    Returns, per node pair, the class and the indexes of the rows behind it."""
    grouped: dict[tuple[str, str], list[tuple[EdgeClass, int]]] = defaultdict(list)
    for i, (left, right, cls) in enumerate(rows):
        a, b = node_of(left), node_of(right)
        if a is None or b is None or a == b:
            continue
        grouped[(a, b) if a < b else (b, a)].append((cls, i))
    out: dict[tuple[str, str], tuple[EdgeClass, list[int]]] = {}
    for pair, items in grouped.items():
        classes = {c for c, _ in items}
        if "cannot_link" in classes:
            cls: EdgeClass = "cannot_link"
        else:
            cls = max(classes, key=_RANK.__getitem__)
            if cls == "strong" and "negative" in classes:
                cls = "contested"
        out[pair] = (cls, [i for _, i in items])
    return out
