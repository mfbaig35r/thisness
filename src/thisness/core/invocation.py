"""Model invocation records (design 05 §3.2, PRD appendix A.5).

Every call to a model is a fact: what was sent (after privacy filtering), what came
back, what it cost, and whether it was usable. Decisions and proposals that rest on
a model's answer cite the invocation, so ``explain`` reaches the exact exchange.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from thisness.core.base import Model
from thisness.core.types import InvocationId, NonEmpty, RunId, StageId

InvocationStatus = Literal["ok", "invalid", "refused", "error", "cached"]


class ModelInvocation(Model):
    invocation_id: InvocationId
    provider: NonEmpty
    model: NonEmpty
    model_build: str | None = None  # what the provider says actually served the call
    prompt_ref: NonEmpty  # ComponentRef of the prompt: text and response schema
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, JsonValue]  # the fields sent, after privacy filtering
    response: JsonValue = None  # the provider's raw response
    output: dict[str, JsonValue] | None = None  # the validated structured answer
    status: InvocationStatus
    error: str | None = None
    attempts: int = Field(default=1, ge=0)
    cached_from: InvocationId | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    run_id: RunId | None = None
    stage_id: StageId | None = None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.status in ("ok", "cached")) != (self.output is not None):
            raise ValueError("an output is present exactly when the call succeeded")
        if (self.status == "cached") != (self.cached_from is not None):
            raise ValueError("cached_from is set exactly for a cached call")
        return self
