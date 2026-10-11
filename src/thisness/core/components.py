"""Versioned components (design 02 §4).

Every pluggable part is identified by kind, name, version and a hash of its
configuration, so the same code with different thresholds is a different
component. Decisions record the refs they used.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from thisness._canonical import digest
from thisness.core.base import Model
from thisness.core.types import Name

ComponentKind = Literal[
    "profile",
    "normalizer",
    "candidate_generator",
    "comparison",
    "constraint",
    "resolver",
    "model",
    "edge_policy",
    "consensus",
    "policy",
    "canonicalizer",
    "prompt",
]


class ComponentRef(Model):
    kind: ComponentKind
    name: Name
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def of(
        cls, kind: ComponentKind, name: str, version: str, config: Mapping[str, Any]
    ) -> ComponentRef:
        return cls(kind=kind, name=name, version=version, config_hash=digest(dict(config)))

    @property
    def ref(self) -> str:
        """The stable string form stored with records: ``kind:name@version#hash12``."""
        return f"{self.kind}:{self.name}@{self.version}#{self.config_hash[:12]}"

    def __str__(self) -> str:
        return self.ref
