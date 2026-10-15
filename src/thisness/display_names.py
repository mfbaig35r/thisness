"""Display names suggested by a model (design 08 §5.2). Not to be confused with
``labels``, which are review answers as training labels (design 04 §2.7).

A model suggests a readable name for entities whose label is written in capitals or
lower case, from the spellings their records use. Each suggestion is a recorded decision
that changes nothing; a person accepting it labels the entity by a commit that cites it,
which a later commit can clear. A suggestion must be a spelling of one of the entity's own
names (the same key, by the profile's value keys) or it is not recorded: casing, spacing
and the legal form's spelling can change, the name cannot.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from thisness.core.components import ComponentRef
from thisness.core.decisions import Actor, LabelSuggestion
from thisness.dossier import _ValueKeys
from thisness.llm import BudgetExhausted, LLMClient
from thisness.llm.prompts import Prompt

if TYPE_CHECKING:
    from thisness.engine import Thisness

BATCH = 20  # entities per call: the work is bound by requests per minute, not tokens
SPELLINGS = 8  # most frequent spellings shown per entity


class SuggestedName(BaseModel):
    ref: str
    name: str = Field(min_length=1, max_length=300)


class SuggestedNames(BaseModel):
    names: list[SuggestedName]


def default_prompt() -> Prompt:
    return Prompt.load("suggest_labels", "1", SuggestedNames)


def needs_name(label: str | None) -> bool:
    """A label written all in capitals or all in lower case."""
    if not label:
        return False
    letters = [c for c in label if c.isalpha()]
    return len(letters) > 3 and (label == label.upper() or label == label.lower())


@dataclass
class LabelAssistant:
    client: LLMClient
    model: str = "claude-sonnet-5"
    prompt: Prompt = field(default_factory=default_prompt)

    @property
    def ref(self) -> ComponentRef:
        return ComponentRef.of("resolver", "label_assistant", "1",
                               {"model": self.model, "prompt": self.prompt.ref.ref})  # fmt: skip


def suggest(engine: Thisness, entity_type: str, assistant: LabelAssistant, *,
            limit: int = 100) -> dict[str, Any]:  # fmt: skip
    """Suggestions for up to ``limit`` entities that need a name and have none suggested
    or accepted. Returns counts: asked, suggested, rejected (not a spelling of the
    entity's names), unanswered."""
    profile = engine.profiles[entity_type]
    keyer = _ValueKeys(profile)
    candidates = [(e, label) for e, label in engine.store.label_candidates(entity_type)
                  if needs_name(label)][:limit]  # fmt: skip
    if not candidates:
        return {"asked": 0, "suggested": 0, "rejected": 0, "unanswered": 0}
    members = engine.store.members_of([e for e, _ in candidates])
    spellings: dict[str, list[str]] = {}
    for entity_id, _ in candidates:
        names = Counter(str((o.attributes or {}).get("name")) for o in
                        engine.store.observations(members.get(entity_id, []))
                        if (o.attributes or {}).get("name"))  # fmt: skip
        spellings[entity_id] = [n for n, _ in names.most_common(SPELLINGS)]
    prompt = assistant.prompt.for_profile(profile)
    engine.store.register_component(assistant.ref, {"model": assistant.model})
    counts = Counter()

    def one(batch: list[tuple[str, str]]) -> list[LabelSuggestion]:
        refs = {f"E{i + 1}": e for i, (e, _) in enumerate(batch)}
        payload = {"entity_type": entity_type,
                   "items": [{"ref": r, "spellings": spellings[e]} for r, e in refs.items()]}  # fmt: skip
        try:
            answer = assistant.client.call(prompt, model=assistant.model, payload=payload)
        except BudgetExhausted:
            counts["unanswered"] += len(batch)
            return []
        if not answer.ok:
            counts["unanswered"] += len(batch)
            return []
        out, seen = [], set()
        for item in answer.parsed.names:
            entity_id = refs.get(item.ref)
            if entity_id is None or entity_id in seen:
                continue
            seen.add(entity_id)
            own = {keyer.key({}, "name", s) for s in spellings[entity_id]}
            if keyer.key({}, "name", item.name) not in own:
                counts["rejected"] += 1
                continue
            out.append(LabelSuggestion(
                decision_id=engine.ids.decision(), actor=Actor(kind="agent", id=assistant.model),
                component_refs=(assistant.ref.ref,), invocation_id=answer.invocation.invocation_id,
                created_at=engine.clock(), entity_id=entity_id, name=item.name.strip()))  # fmt: skip
        counts["unanswered"] += len(batch) - len(seen)
        return out

    batches = [candidates[i : i + BATCH] for i in range(0, len(candidates), BATCH)]
    with ThreadPoolExecutor(max_workers=max(1, assistant.client.policy.concurrency)) as pool:
        suggestions = [s for found in pool.map(one, batches) for s in found]
    engine.store.record_decisions(suggestions)
    return {"asked": len(candidates), "suggested": len(suggestions),
            "rejected": counts["rejected"], "unanswered": counts["unanswered"]}  # fmt: skip
