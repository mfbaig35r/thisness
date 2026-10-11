"""What a model may see (design 05 §3.5, PRD appendix A.14).

One function decides, for every request and for the dossier's LLM context: each
attribute's ``external_model_policy`` says whether its value is sent as is, hashed,
redacted, sent only to a local model, or never sent.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from thisness._canonical import canonical_json
from thisness.profiles.base import AttributeDefinition

OMIT = object()


def shown(value: Any, policy: str, *, local: bool = False) -> Any:
    """The value as a model may see it, or ``OMIT`` when it may not see it at all."""
    if policy == "forbidden" or (policy == "local_only" and not local):
        return OMIT
    if policy == "redact":
        return "[redacted]"
    if policy == "hash":
        text = value if isinstance(value, str) else canonical_json(value)
        return "#" + hashlib.sha256(text.encode()).hexdigest()[:12]
    return value


def filter_attributes(
    attributes: Mapping[str, Any],
    definitions: Mapping[str, AttributeDefinition],
    *,
    local: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Attributes a model may see, and the names of those withheld. Undeclared
    attributes are withheld: a profile has to say a field may leave."""
    out, withheld = {}, []
    for name in sorted(attributes):
        value = attributes[name]
        if value is None or value == "" or value == []:
            continue
        definition = definitions.get(name)
        policy = definition.external_model_policy if definition else "forbidden"
        if isinstance(value, list):
            items = [shown(v, policy, local=local) for v in value]
            items = [v for v in items if v is not OMIT]
            result: Any = items if items else OMIT
        else:
            result = shown(value, policy, local=local)
        if result is OMIT:
            withheld.append(name)
        else:
            out[name] = result
    return out, withheld
