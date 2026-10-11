"""Scoring candidate pairs (design 03 §7-8).

``score_pairs`` turns candidate pairs into one row each: comparison levels, the
Fellegi-Sunter weight with its common-name adjustment, the posterior under the
model's match rate, any cannot-link reason, any decisive rule, and whether the
pair carries a relationship signal. ``evidence_items`` turns the same rows into
evidence records. Edge classes and consensus are applied later (design 01).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

import pyarrow as pa

from thisness.comparisons.base import FeatureView
from thisness.core.evidence import EvidenceItem, Score
from thisness.fellegi_sunter import FSModel
from thisness.profiles.base import ResolutionProfile
from thisness.training import EMPTY, pattern


def _schema(profile: ResolutionProfile) -> pa.Schema:
    return pa.schema(
        [
            ("left_id", pa.string()),
            ("right_id", pa.string()),
            *[(f"level_{c.name}", pa.string()) for c in profile.comparisons],
            ("fs_weight", pa.float64()),
            ("tf_adjustment", pa.float64()),
            ("weight", pa.float64()),
            ("posterior", pa.float64()),
            ("cannot_link", pa.bool_()),
            ("cannot_link_reason", pa.string()),
            ("rule", pa.string()),
            ("rule_verdict", pa.string()),
            ("relationship_signal", pa.bool_()),
        ]
    )


def score_pairs(
    profile: ResolutionProfile,
    model: FSModel,
    features: Mapping[str, FeatureView],
    pairs: Iterable[tuple[str, str]],
    *,
    frequencies: Mapping[str, float] | None = None,
    floor: float | None = None,
    tables: Mapping[str, tuple[Mapping[str, float], float]] | None = None,
) -> pa.Table:
    """``frequencies`` and ``floor`` serve the model's first common-value adjustment;
    ``tables`` maps each adjustment's feature to its frequencies and floor."""
    if [c.name for c in profile.comparisons] != list(model.comparisons):
        raise ValueError("model was trained on different comparisons than this profile has")
    floor = floor if floor is not None else 1.0 / max(len(features), 1)
    tables = dict(tables or {})
    if frequencies is not None and model.tfs:
        tables.setdefault(model.tfs[0].feature, (frequencies, floor))
    specs = [(t, tables[t.feature]) for t in model.tfs if t.feature in tables]
    rows = []
    for left, right in pairs:
        a, b = features.get(left, EMPTY), features.get(right, EMPTY)
        p = pattern(profile, a, b)
        labels = {
            c.name: None if level is None else c.levels[level]
            for c, level in zip(profile.comparisons, p, strict=True)
        }
        fs_weight = model.weight(p)
        adjustment = 0.0
        for tf, (freqs, tf_floor) in specs:
            va, vb = a.get(tf.feature), b.get(tf.feature)
            adjustment += model.tf_adjustment(
                p, va[0] if va else None, vb[0] if vb else None, freqs, tf_floor, spec=tf
            )
        weight = fs_weight + adjustment
        reason = next((r for c in profile.constraints if (r := c.cannot_link(a, b))), None)
        rule, verdict = next(
            ((r.name, v) for r in profile.rules if (v := r.apply(labels)) is not None),
            (None, None),
        )
        rows.append(
            {
                "left_id": left,
                "right_id": right,
                **{f"level_{name}": label for name, label in labels.items()},
                "fs_weight": fs_weight,
                "tf_adjustment": adjustment,
                "weight": weight,
                "posterior": model.posterior(weight),
                "cannot_link": reason is not None,
                "cannot_link_reason": reason,
                "rule": rule,
                "rule_verdict": verdict,
                "relationship_signal": bool(profile.relationship_signals(labels)),
            }
        )
    return pa.Table.from_pylist(rows, schema=_schema(profile))


def _strength(bits: float) -> str:
    size = abs(bits)
    return (
        "decisive" if size >= 10 else "strong" if size >= 5 else "moderate" if size >= 2 else "weak"
    )


def _protect(values: list[str], policy: str) -> list[str] | None:
    if policy == "redact":
        return None
    if policy == "hash":
        return [hashlib.sha256(v.encode()).hexdigest()[:16] for v in values]
    return values


def evidence_items(
    profile: ResolutionProfile,
    model: FSModel,
    scored: pa.Table,
    features: Mapping[str, FeatureView],
    *,
    new_id: Callable[[], str],
    created_at: datetime,
    run_id: str | None = None,
    stage_id: str | None = None,
) -> list[EvidenceItem]:
    """One item per non-null comparison per pair, plus one per constraint or rule that
    fired. Inputs follow the strictest log policy of the attributes compared."""
    out: list[EvidenceItem] = []
    model_ref = model.ref.ref
    common: dict[str, Any] = {"run_id": run_id, "stage_id": stage_id, "created_at": created_at}
    for row in scored.to_pylist():
        a = features.get(row["left_id"], EMPTY)
        b = features.get(row["right_id"], EMPTY)
        pair = {"left_id": row["left_id"], "right_id": row["right_id"]}
        for comparison in profile.comparisons:
            label = row[f"level_{comparison.name}"]
            if label is None:
                continue
            bits = model.level_weight(comparison.name, comparison.levels.index(label))
            policy = profile.log_policy(comparison)
            inputs: dict[str, Any] = {}
            for side, view in (("left", a), ("right", b)):
                for feature in comparison.features:
                    shown = _protect(list(view.get(feature, ())), policy)
                    if shown:
                        inputs[f"{side}.{feature}"] = shown
            out.append(
                EvidenceItem(
                    evidence_id=new_id(),
                    **pair,
                    kind=f"compare.{comparison.name}",
                    attribute=comparison.name,
                    level=label,
                    direction=comparison.polarity[label],
                    strength=_strength(bits),
                    value=label,
                    score=Score(kind="fs_level_weight", value=bits, model_ref=model_ref),
                    method_ref=comparison.ref.ref,
                    inputs=inputs,
                    **common,
                )
            )
        if row["cannot_link"]:
            constraint = next(c for c in profile.constraints if c.cannot_link(a, b))
            out.append(
                EvidenceItem(
                    evidence_id=new_id(),
                    **pair,
                    kind=f"constraint.{constraint.name}",
                    direction="contradicts",
                    strength="decisive",
                    value=row["cannot_link_reason"],
                    method_ref=constraint.ref.ref,
                    **common,
                )
            )
        if row["rule"] is not None:
            rule = next(r for r in profile.rules if r.name == row["rule"])
            out.append(
                EvidenceItem(
                    evidence_id=new_id(),
                    **pair,
                    kind=f"rule.{rule.name}",
                    direction="supports" if row["rule_verdict"] == "MATCH" else "contradicts",
                    strength="decisive",
                    value=row["rule_verdict"],
                    score=Score(kind="rule", value=rule.name),
                    method_ref=rule.ref.ref,
                    **common,
                )
            )
    return out
