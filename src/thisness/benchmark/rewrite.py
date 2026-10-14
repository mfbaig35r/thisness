"""A rewrite-and-hash baseline for name-only resolution (design 08, measured first).

The method common in spend-analysis pipelines, reproduced to be measured against the
name-only plan (design 05 §6):

1. Each distinct name is keyed by its letters and digits, upper-cased.
2. One to three passes over overlapping chunks: a model rewrites every name in a chunk
   to a cleaned, consolidated name, giving names it judges to be one organization the
   same text. Pass 1 sees the names in input order; later passes see the previous pass's
   names, ordered by key hash (as built) or by name.
3. Each name's last answered rewrite (else the original), keyed the same way, is its
   intermediate group: two records are one organization when their rewrites are equal.
4. For each group, a model picks one standardized name from its variations; groups
   whose standardized names key alike merge.

Identity is string equality of model output: there are no pairwise comparisons, votes
or reviews. The prompts here are written for this baseline.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from thisness.benchmark.evaluate import ClusterMetrics, cluster_metrics
from thisness.llm import BudgetExhausted
from thisness.llm.prompts import Prompt
from thisness.llm.providers.function import FunctionProvider

_NOT_ALNUM = re.compile(r"[^A-Z0-9]")


def key(name: str) -> str:
    return _NOT_ALNUM.sub("", name.upper())


class Rewrite(BaseModel):
    ref: str
    name: str


class RewriteAnswer(BaseModel):
    items: list[Rewrite]


class Standardized(BaseModel):
    name: str


REWRITE = Prompt(
    "baseline_rewrite_names",
    "1",
    system="""You clean and consolidate supplier names taken from purchasing and accounts-payable data.

For every item, write a cleaned name:
- Remove what is not part of the name: account, vendor, store or reference numbers; payment-processor prefixes; internal notes.
- Fix spacing and casing, and expand obvious abbreviations (Mfg to Manufacturing) when that does not change the name.
- Keep legal forms (Ltd, Inc, GmbH, PLC).

Then consolidate: when items in the list very likely name the same organization, give them exactly the same cleaned name. Differences of punctuation, casing, spacing, abbreviation or small typos usually mean the same organization; a different word usually means a different one.

Return every item once, by its ref, with its cleaned name.""",
    user="{payload}",
    response=RewriteAnswer,
)

STANDARDIZE = Prompt(
    "baseline_standardize_name",
    "1",
    system="""You pick one standardized name for a supplier from the ways different records wrote it.
Return the clearest full form of the name, with its legal form, in normal casing. Use only the variations given.""",
    user="{payload}",
    response=Standardized,
)

SIZES = (50, 75, 100)  # chunk size per pass
OVERLAP = 10


@dataclass
class RewriteResult:
    assignment: dict[str, str]  # record id -> group
    metrics: ClusterMetrics
    stats: dict[str, Any] = field(default_factory=dict)


def _chunks(items: list[Any], size: int, overlap: int) -> list[list[Any]]:
    step = max(1, size - overlap)
    out, start = [], 0
    while start < len(items):
        out.append(items[start : start + size])
        if start + size >= len(items):
            break
        start += step
    return out


def run(benchmark, client, *, model: str, passes: int = 3, order: str = "as_built",
        concurrency: int = 16, shuffle_seed: int | None = 13) -> RewriteResult:  # fmt: skip
    """Resolve a names-only benchmark by rewriting and hashing; score it like any run.
    Records are shuffled first (``shuffle_seed``): the generators emit them organization
    by organization, which would hand pass 1 every variant in one chunk; an AP extract
    is in no such order."""
    records = [(r.record_id, r.attributes["name"]) for r in benchmark.records]
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(records)
    display: dict[str, str] = {}
    for _, name in records:
        display.setdefault(key(name), name)
    keys = list(display)  # input order: the first record carrying each key
    calls = Counter()

    def ask(prompt: Prompt, payload: dict) -> Any:
        try:
            answer = client.call(prompt, model=model, payload=payload)
        except BudgetExhausted:
            calls["budget"] += 1
            return None
        calls["calls"] += 1
        if not answer.ok:
            calls["failed"] += 1
            return None
        return answer.parsed

    passes_out: list[dict[str, str]] = []
    current = {k: display[k] for k in keys}
    for p in range(passes):
        if p == 0:
            sequence = keys
        elif order == "as_built":  # the next pass's input, sorted by its key's hash
            sequence = sorted(current, key=lambda k: hashlib.sha256(k.encode()).hexdigest())
        else:
            sequence = sorted(current, key=lambda k: current[k].upper())
        batches = _chunks(sequence, SIZES[min(p, len(SIZES) - 1)], OVERLAP)

        def one(batch: list[str], current: dict[str, str] = current) -> list[tuple[str, str]]:
            refs = {f"N{i + 1}": k for i, k in enumerate(batch)}
            parsed = ask(
                REWRITE, {"items": [{"ref": r, "name": current[k]} for r, k in refs.items()]}
            )
            if parsed is None:
                return []
            out = []
            for item in parsed.items:
                if item.ref in refs and item.name.strip():
                    out.append((refs[item.ref], item.name.strip()))
            return out

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            answers = list(pool.map(one, batches))
        this_pass: dict[str, str] = {}
        for answer in answers:  # first answer per key, in chunk order
            for k, name in answer:
                this_pass.setdefault(k, name)
        passes_out.append(this_pass)
        current = {k: this_pass[k] for k in current if k in this_pass}

    # Intermediate groups: the last answered rewrite, else the original name.
    group_of: dict[str, str] = {}
    variations: dict[str, set[str]] = defaultdict(set)
    for k in keys:
        final = next((out[k] for out in reversed(passes_out) if k in out), display[k])
        g = key(final) or k
        group_of[k] = g
        variations[g] |= {display[k], *(out[k] for out in passes_out if k in out)}

    def standardize(g: str) -> tuple[str, str]:
        names = sorted(variations[g])
        parsed = ask(STANDARDIZE, {"variations": names})
        return g, (key(parsed.name) or g) if parsed is not None else g

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        final_of = dict(pool.map(standardize, sorted(variations)))

    assignment = {rid: final_of[group_of[key(name)]] for rid, name in records}
    metrics = cluster_metrics(assignment, benchmark.truth, benchmark.kind)
    stats = {
        "names": len(records), "keys": len(keys),
        "intermediate_groups": len(variations), "final_groups": len(set(final_of.values())),
        "answered_per_pass": [len(o) for o in passes_out],
        "calls": calls["calls"], "failed": calls["failed"], "budget_stops": calls["budget"],
        "spent_usd": round(client.spent_usd, 6), "order": order, "passes": passes, "model": model,
    }  # fmt: skip
    return RewriteResult(assignment, metrics, stats)


def oracle(benchmark) -> FunctionProvider:
    """A model that consolidates only what it sees together, from the truth (design 05
    §11.5). In a rewrite chunk, items of one organization get the name of the first of
    them; an item alone with its organization keeps its name. Standardization returns a
    group's own first variation, so it merges groups only when those coincide, as the
    real runs found (1,094 groups before it, 1,092 after). A name is read as the organization most of its
    records belong to, as a model cannot tell two companies of one name apart."""
    owners: dict[str, Counter] = defaultdict(Counter)
    for r in benchmark.records:
        owners[key(r.attributes["name"])][benchmark.truth[r.record_id]] += 1

    def org_of(name: str) -> str | None:
        found = owners.get(key(name))
        return found.most_common(1)[0][0] if found else None

    def answer(request) -> dict:
        payload = json.loads(request.messages[0].content)
        if "variations" in payload:
            return {"name": payload["variations"][0]}
        first: dict[str | None, str] = {}
        items = []
        for item in payload["items"]:
            org = org_of(item["name"]) or item["ref"]
            items.append({"ref": item["ref"], "name": first.setdefault(org, item["name"])})
        return {"items": items}

    return FunctionProvider(answer, name="oracle")
