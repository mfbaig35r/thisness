"""Anthropic (Claude) through the official SDK: ``pip install thisness[anthropic]``.

Structured output by forced tool use: the answer schema is a tool's input schema,
and the request requires that tool, so the reply is the tool call's input.
"""

from __future__ import annotations

from typing import Any

from thisness.llm.types import ModelRequest, ModelResponse, ProviderError


class AnthropicProvider:
    name = "anthropic"
    local = False

    def __init__(self, *, api_key: str | None = None, client: Any = None,
                 max_retries: int = 3, timeout: float = 120.0) -> None:  # fmt: skip
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise ImportError("AnthropicProvider needs: pip install 'thisness[anthropic]'") \
                    from exc  # fmt: skip
            client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries,
                                         timeout=timeout)  # fmt: skip
        self.client = client

    def invoke(self, request: ModelRequest) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [m.model_dump() for m in request.messages],
            "tools": [{"name": request.schema_name, "description": "Return your answer.",
                       "input_schema": request.response_schema}],
            "tool_choice": {"type": "tool", "name": request.schema_name},
        }  # fmt: skip
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        try:
            response = self.client.messages.create(**kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            retryable = status is None or status == 429 or status >= 500
            raise ProviderError(f"anthropic: {type(exc).__name__}: {exc}", retryable=retryable,
                                raw={"status": status}) from exc  # fmt: skip
        raw = response.model_dump(mode="json") if hasattr(response, "model_dump") else None
        usage = getattr(response, "usage", None)
        output = next((block.input for block in response.content
                       if getattr(block, "type", None) == "tool_use"), None)  # fmt: skip
        return ModelResponse(
            output=output,
            raw=raw,
            refused=getattr(response, "stop_reason", None) == "refusal" or output is None,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            model_build=getattr(response, "model", None),
        )
