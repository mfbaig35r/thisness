"""Prompts as versioned components (design 05 §3.4, PRD appendix A.23 invariant 14).

A prompt is a text file shipped in the package and a Pydantic model for its answer.
Its ``ComponentRef`` hashes both, so changing a word of the prompt or a field of the
answer is a new version: a cached answer to the old one is never reused, and every
invocation records exactly which prompt produced it.

File format: a ``=== system ===`` section and a ``=== user ===`` section. The user
section carries a ``{payload}`` placeholder, replaced by the request's fields as JSON.

A profile adds its domain's guidance to a prompt (``with_hints``, design 06 §6): what
counts as one name for organizations, people or plant taxa. The guidance is part of
the system text, so it is part of the prompt's ref: changing it is a new prompt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from pydantic import BaseModel

from thisness.core.components import ComponentRef
from thisness.llm.types import Message

SYSTEM, USER = "=== system ===", "=== user ==="


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    system: str
    user: str
    response: type[BaseModel]
    ref: ComponentRef = field(init=False, compare=False)

    def __post_init__(self) -> None:
        if "{payload}" not in self.user:
            raise ValueError(f"prompt {self.name} has no {{payload}} placeholder")
        config = {"system": _sha(self.system), "user": _sha(self.user),
                  "schema": self.response.model_json_schema()}  # fmt: skip
        object.__setattr__(self, "ref", ComponentRef.of("prompt", self.name, self.version, config))

    def with_hints(self, entity_type: str, hints: str | None) -> Prompt:
        """This prompt with a profile's guidance for its entity type appended."""
        if not hints or not hints.strip():
            return self
        system = f"{self.system}\n\nGuidance for {entity_type} records:\n{hints.strip()}"
        return Prompt(self.name, self.version, system, self.user, self.response)

    def for_profile(self, profile: Any) -> Prompt:
        if profile is None:
            return self
        return self.with_hints(profile.entity_type, getattr(profile, "prompt_hints", None))

    @property
    def schema(self) -> dict[str, Any]:
        return self.response.model_json_schema()

    def messages(self, payload: dict[str, Any]) -> tuple[Message, ...]:
        body = json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False)
        return (Message(role="user", content=self.user.replace("{payload}", body)),)

    @classmethod
    def parse(cls, name: str, version: str, text: str, response: type[BaseModel]) -> Prompt:
        if SYSTEM not in text or USER not in text:
            raise ValueError(f"prompt {name} needs {SYSTEM} and {USER} sections")
        system, user = text.split(SYSTEM, 1)[1].split(USER, 1)
        return cls(name, version, system.strip(), user.strip(), response)

    @classmethod
    def load(cls, name: str, version: str, response: type[BaseModel]) -> Prompt:
        """A prompt shipped in ``thisness/llm/prompts/<name>.v<version>.md``."""
        resource = resources.files("thisness.llm") / "prompts" / f"{name}.v{version}.md"
        return cls.parse(name, version, resource.read_text(encoding="utf-8"), response)
