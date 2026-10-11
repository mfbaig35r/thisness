"""Fellegi-Sunter (design 03 §8).

Weights are ``log2(m / u)`` per comparison level, summed over non-null
comparisons. u comes from random pairs, which blocking cannot bias. m and the
match rate come from EM over candidate pairs, run in blocking sessions: a session
never learns m for a comparison its generators blocked on. Each comparison's m is
pooled across the sessions that could learn it, weighted by each session's
expected number of matches.

Everything here is pure: it works on level-pattern counts. ``thisness.training``
gathers those counts from a store.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from thisness.core.components import ComponentRef

Pattern = tuple[int | None, ...]


@dataclass(frozen=True)
class TermFrequency:
    """Common-name adjustment on one comparison (design 03 §8.5).

    The exact level gets ``log2(u_exact / f(value))``: rare values gain, common ones
    lose. Fuzzy levels get only the penalty, using the more common of the two values.
    ``exact_level`` is whichever level the value's frequency stands in for: for taxa,
    the recombination level, where one epithet in two genera is strong evidence when
    the epithet is rare and little when it is *japonica* (design 06 §10.4).
    """

    comparison: str
    feature: str
    exact_level: int = 0
    fuzzy_levels: tuple[int, ...] = ()
    # How a value's frequency is counted: over entities (organizations, where a heavy
    # supplier has dozens of records) or over observations (people, where a first pass
    # would merge same-name strangers and make their name look rare).
    count: str = "entity"
    # False: the adjustment only discounts a common value, never rewards a rare one. A
    # postcode's baseline is set by formation agents holding thousands of companies, so
    # an ordinary group head office looked rarer than average and earned a bonus that
    # merged the group's siblings (design 07 §13.4).
    boost: bool = True


@dataclass(frozen=True)
class Session:
    name: str
    blocked: frozenset[str]  # comparison names this session's generators blocked on
    patterns: Counter[Pattern]


@dataclass(frozen=True)
class FSModel:
    entity_type: str
    comparisons: tuple[str, ...]
    levels: Mapping[str, tuple[str, ...]]
    m: Mapping[str, tuple[float, ...]]
    u: Mapping[str, tuple[float, ...]]
    lam: float  # match rate among candidate pairs
    caps: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    # One adjustment, or several on different comparisons (a taxon's name and epithet).
    tf: TermFrequency | tuple[TermFrequency, ...] | None = None
    sessions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # diagnostics
    unlearned: tuple[str, ...] = ()  # comparisons no session could learn m for
    m_floor: float = 0.0
    version: str = "1"

    # -- identity ---------------------------------------------------------------------

    def config(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "comparisons": list(self.comparisons),
            "levels": {c: list(v) for c, v in self.levels.items()},
            "m": {c: list(v) for c, v in self.m.items()},
            "u": {c: list(v) for c, v in self.u.items()},
            "lam": self.lam,
            "caps": {c: dict(v) for c, v in self.caps.items()},
            # One adjustment is written as before, so existing models keep their ref.
            "tf": None
            if self.tf is None
            else _tf_config(self.tf)
            if isinstance(self.tf, TermFrequency)
            else [_tf_config(t) for t in self.tf],
            "sessions": {k: dict(v) for k, v in self.sessions.items()},
            "unlearned": list(self.unlearned),
            "m_floor": self.m_floor,
        }

    @classmethod
    def from_config(cls, config: Mapping[str, Any], version: str = "1") -> FSModel:
        tf = config.get("tf")
        return cls(
            entity_type=config["entity_type"],
            comparisons=tuple(config["comparisons"]),
            levels={c: tuple(v) for c, v in config["levels"].items()},
            m={c: tuple(v) for c, v in config["m"].items()},
            u={c: tuple(v) for c, v in config["u"].items()},
            lam=config["lam"],
            caps={c: dict(v) for c, v in config.get("caps", {}).items()},
            tf=None
            if tf is None
            else _tf_from(tf)
            if isinstance(tf, dict)
            else tuple(_tf_from(t) for t in tf),
            sessions=config.get("sessions", {}),
            unlearned=tuple(config.get("unlearned", ())),
            m_floor=config.get("m_floor", 0.0),
            version=version,
        )

    @property
    def ref(self) -> ComponentRef:
        return ComponentRef.of(
            "model", f"fellegi_sunter.{self.entity_type}", self.version, self.config()
        )

    # -- scoring ------------------------------------------------------------------------

    def level_weight(self, comparison: str, level: int, *, capped: bool = True) -> float:
        weight = math.log2(self.m[comparison][level] / self.u[comparison][level])
        if capped:
            cap = self.caps.get(comparison, {}).get(self.levels[comparison][level])
            if cap is not None:
                weight = min(weight, cap)
        return weight

    def weight(self, pattern: Pattern) -> float:
        return sum(
            self.level_weight(c, level)
            for c, level in zip(self.comparisons, pattern, strict=True)
            if level is not None
        )

    @property
    def tfs(self) -> tuple[TermFrequency, ...]:
        if self.tf is None:
            return ()
        return (self.tf,) if isinstance(self.tf, TermFrequency) else tuple(self.tf)

    def tf_adjustment(
        self,
        pattern: Pattern,
        value_a: str | None,
        value_b: str | None,
        frequencies: Mapping[str, float],
        floor: float,
        spec: TermFrequency | None = None,
    ) -> float:
        """Bits to add for the common-value adjustment ``spec`` (the first, by default);
        0 when it does not apply."""
        tf = spec or (self.tfs[0] if self.tfs else None)
        if tf is None or value_a is None or value_b is None:
            return 0.0
        level = pattern[self.comparisons.index(tf.comparison)]
        u_exact = self.u[tf.comparison][tf.exact_level]
        if level == tf.exact_level:
            bits = math.log2(u_exact / max(frequencies.get(value_a, floor), floor))
            return bits if tf.boost else min(0.0, bits)
        if level in tf.fuzzy_levels:
            f = max(frequencies.get(value_a, floor), frequencies.get(value_b, floor), floor)
            return min(0.0, math.log2(u_exact / f))
        return 0.0

    def posterior(self, weight: float) -> float:
        odds = math.log2(self.lam / (1 - self.lam)) + weight
        return 1 / (1 + 2 ** (-odds)) if odds > -1000 else 0.0

    def weight_for_posterior(self, probability: float) -> float:
        """The weight at which the posterior reaches ``probability``: the label-free
        T_high of design 03 §11."""
        return math.log2(probability / (1 - probability)) - math.log2(self.lam / (1 - self.lam))


def _tf_config(tf: TermFrequency) -> dict[str, Any]:
    return {
        "comparison": tf.comparison,
        "feature": tf.feature,
        "exact_level": tf.exact_level,
        "fuzzy_levels": list(tf.fuzzy_levels),
        **({"count": tf.count} if tf.count != "entity" else {}),
        **({"boost": False} if not tf.boost else {}),
    }


def _tf_from(config: Mapping[str, Any]) -> TermFrequency:
    return TermFrequency(config["comparison"], config["feature"], config["exact_level"],
                         tuple(config["fuzzy_levels"]), config.get("count", "entity"),
                         config.get("boost", True))  # fmt: skip


# --- estimation ------------------------------------------------------------------------------


def level_distribution(
    patterns: Counter[Pattern], index: int, n_levels: int, smoothing: float = 1.0
) -> tuple[float, ...]:
    counts = [smoothing] * n_levels
    for pattern, n in patterns.items():
        if pattern[index] is not None:
            counts[pattern[index]] += n
    total = sum(counts)
    return tuple(c / total for c in counts)


def _initial_m(n_levels: int) -> list[float]:
    if n_levels == 1:
        return [1.0]
    return [0.9] + [0.1 / (n_levels - 1)] * (n_levels - 1)


def em(
    patterns: Counter[Pattern],
    u: Sequence[Sequence[float]],
    active: Sequence[int],
    m0: Sequence[Sequence[float]],
    *,
    lam: float = 0.2,
    fit_m: bool = True,
    max_iter: int = 500,
    tol: float = 1e-9,
) -> tuple[list[list[float]], float]:
    """EM over pattern counts with u fixed. Only ``active`` comparisons enter the
    likelihood; with ``fit_m=False`` only the match rate is estimated."""
    m = [list(row) for row in m0]
    total = sum(patterns.values())
    if total == 0:
        return m, lam
    for _ in range(max_iter):
        num = [[1e-6] * len(row) for row in m]
        den = [1e-6 * len(row) for row in m]
        p_sum = 0.0
        for pattern, n in patterns.items():
            lm, lu = math.log(lam), math.log(1 - lam)
            for c in active:
                level = pattern[c]
                if level is not None:
                    lm += math.log(m[c][level])
                    lu += math.log(u[c][level])
            diff = lu - lm
            p = 0.0 if diff > 700 else 1 / (1 + math.exp(diff))
            p_sum += p * n
            if fit_m:
                for c in active:
                    level = pattern[c]
                    if level is not None:
                        num[c][level] += p * n
                        den[c] += p * n
        new_lam = min(max(p_sum / total, 1e-9), 1 - 1e-9)
        new_m = (
            [[num[c][i] / den[c] for i in range(len(m[c]))] if c in active else m[c]
             for c in range(len(m))]
            if fit_m
            else m
        )  # fmt: skip
        delta = abs(new_lam - lam)
        if fit_m:
            delta += max((abs(x - y) for c in active for x, y in zip(new_m[c], m[c], strict=True)),
                         default=0.0)  # fmt: skip
        m, lam = new_m, new_lam
        if delta < tol:
            break
    return m, lam


def fit(
    *,
    entity_type: str,
    comparisons: Sequence[str],
    levels: Mapping[str, tuple[str, ...]],
    random_patterns: Counter[Pattern],
    sessions: Sequence[Session],
    caps: Mapping[str, Mapping[str, float]] | None = None,
    tf: TermFrequency | tuple[TermFrequency, ...] | None = None,
    m_floor: float = 1e-3,
    version: str = "1",
    prior: FSModel | None = None,
) -> FSModel:
    """``m_floor`` keeps any level's m at or above that probability. EM on clean data
    learns m = 0 for disagreements that never occur among its matches (a second
    domain, a mistyped country), and the weight goes to about -28 bits: one such
    disagreement would veto any match on real data. The default floor caps a single
    disagreement near -10 bits."""
    names = list(comparisons)
    sizes = [len(levels[c]) for c in names]
    u = [level_distribution(random_patterns, i, sizes[i]) for i in range(len(names))]
    m0 = [_initial_m(k) for k in sizes]

    learned: dict[int, list[tuple[float, list[float]]]] = {i: [] for i in range(len(names))}
    diagnostics: dict[str, dict[str, Any]] = {}
    for session in sessions:
        active = [i for i, c in enumerate(names) if c not in session.blocked]
        n = sum(session.patterns.values())
        if n == 0 or not active:
            continue
        m_s, lam_s = em(session.patterns, u, active, m0)
        diagnostics[session.name] = {
            "pairs": n,
            "lam": lam_s,
            "blocked": sorted(session.blocked),
        }
        for i in active:
            learned[i].append((lam_s * n, m_s[i]))

    m: list[list[float]] = []
    unlearned = []
    borrowed = []
    for i, c in enumerate(names):
        weights = [w for w, _ in learned[i] if w > 0]
        if not weights:
            # No session could learn it: a prior model's m (the starter's) is a better
            # guess than an uninformed one, when it has the same comparison and levels.
            if prior is not None and tuple(prior.levels.get(c, ())) == tuple(levels[c]):
                borrowed.append(c)
                m.append(list(prior.m[c]))
            else:
                unlearned.append(c)
                m.append(list(m0[i]))
            continue
        total = sum(weights)
        m.append(
            [sum(w * row[k] for w, row in learned[i] if w > 0) / total for k in range(sizes[i])]
        )
    m = [_floored(row, m_floor) for row in m]
    if borrowed:
        diagnostics["borrowed_from_prior"] = {"comparisons": borrowed,
                                              "prior": prior.ref.ref}  # fmt: skip

    # The match rate among all candidates, with m and u fixed.
    union: Counter[Pattern] = Counter()
    for session in sessions:
        union.update(session.patterns)
    _, lam = em(union, u, list(range(len(names))), m, fit_m=False)

    return FSModel(
        entity_type=entity_type,
        comparisons=tuple(names),
        levels={c: tuple(levels[c]) for c in names},
        m={c: tuple(m[i]) for i, c in enumerate(names)},
        u={c: tuple(u[i]) for i, c in enumerate(names)},
        lam=lam,
        caps={c: dict(v) for c, v in (caps or {}).items()},
        tf=tf,
        sessions=diagnostics,
        unlearned=tuple(unlearned),
        m_floor=m_floor,
        version=version,
    )


def _floored(row: Sequence[float], floor: float) -> list[float]:
    """Raise every probability to at least ``floor``, taking the mass from the levels
    above it so the row still sums to 1."""
    if floor <= 0:
        return list(row)
    low = [i for i, p in enumerate(row) if p < floor]
    if not low:
        return list(row)
    deficit = sum(floor - row[i] for i in low)
    high_mass = sum(p for i, p in enumerate(row) if i not in low)
    return [floor if i in low else p - deficit * p / high_mass for i, p in enumerate(row)]
