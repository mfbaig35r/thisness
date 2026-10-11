"""The provider contract (design 05 §3.1, PRD appendix A.4).

Thisness talks to models only through ``ModelProvider``. Adapters translate a
``ModelRequest`` into one provider's API, ask for structured output with that
provider's native mechanism, and return the parsed answer with usage. They do not
validate the answer against the response model, cache, or record anything: the
client does, the same way for every provider.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field, JsonValue

from thisness.core.base import Model


class Message(Model):
    role: Literal["user", "assistant"]
    content: str


class ModelRequest(Model):
    model: str
    system: str
    messages: tuple[Message, ...] = Field(min_length=1)
    schema_name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    response_schema: dict[str, JsonValue]  # JSON Schema of the answer
    max_output_tokens: int = Field(default=4096, ge=1)
    temperature: float | None = None  # None: the provider's default


class ModelResponse(Model):
    output: dict[str, JsonValue] | None  # the structured answer, as the model gave it
    raw: JsonValue = None  # the provider's response, for the record
    refused: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    model_build: str | None = None


class ProviderError(Exception):
    """The call did not produce a response: network, rate limit, server error."""

    def __init__(self, message: str, *, retryable: bool = False, raw: Any = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.raw = raw


@runtime_checkable
class ModelProvider(Protocol):
    name: str
    local: bool  # runs on infrastructure the user controls; may see local_only attributes

    def invoke(self, request: ModelRequest) -> ModelResponse: ...
