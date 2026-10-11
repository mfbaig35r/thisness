"""Any server speaking the OpenAI chat completions API: OpenAI itself, vLLM, Ollama,
open-lease deployments. ``pip install thisness[openai]`` (it needs only httpx).

Mark a provider ``local=True`` when it runs on infrastructure you control; only a
local provider is sent attributes whose policy is ``local_only`` (design 05 §3.5).

OpenAI's own API takes the output limit as ``max_completion_tokens``, and its reasoning
models reject ``max_tokens``; most other servers take ``max_tokens``. The field follows
the host unless ``token_limit_field`` says otherwise. A reasoning model spends its
hidden reasoning inside that limit, so an answer cut off there is reported as such.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from thisness.llm.types import ModelRequest, ModelResponse, ProviderError


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, *, api_key: str | None = None, local: bool = False,
                 name: str = "openai_compatible", timeout: float = 120.0,
                 client: Any = None, token_limit_field: str | None = None) -> None:  # fmt: skip
        if client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise ImportError("OpenAICompatibleProvider needs: pip install 'thisness[openai]'") \
                    from exc  # fmt: skip
            client = httpx.Client(timeout=timeout)
        self.client = client
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.name = name
        self.local = local
        host = urlparse(base_url).hostname or ""
        self.token_limit_field = token_limit_field or (
            "max_completion_tokens" if host == "api.openai.com" else "max_tokens"
        )

    def invoke(self, request: ModelRequest) -> ModelResponse:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": "system", "content": request.system},
                         *(m.model_dump() for m in request.messages)],
            self.token_limit_field: request.max_output_tokens,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": request.schema_name, "schema": request.response_schema}},
        }  # fmt: skip
        if request.temperature is not None:
            body["temperature"] = request.temperature
        try:
            response = self.client.post(self.url, json=body, headers=self.headers)
        except Exception as exc:
            raise ProviderError(f"{self.name}: {type(exc).__name__}: {exc}",
                                retryable=True) from exc  # fmt: skip
        # Check the status before the body: some gateways answer a rate limit with a 200-
        # shaped error or an empty choice list.
        if response.status_code != 200:
            raise ProviderError(f"{self.name}: HTTP {response.status_code}: {response.text[:500]}",
                                retryable=response.status_code in (408, 429)
                                or response.status_code >= 500,
                                raw={"status": response.status_code})  # fmt: skip
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.name}: response has no choices", retryable=True, raw=data)
        message = choices[0].get("message") or {}
        if not message.get("content") and choices[0].get("finish_reason") == "length":
            raise ProviderError(
                f"{self.name}: the answer was cut off at {request.max_output_tokens} output "
                "tokens (a reasoning model spends its reasoning inside that limit); raise "
                "max_output_tokens or send fewer items per call",
                retryable=False,
                raw=data,
            )
        output = None
        if message.get("content"):
            try:
                parsed = json.loads(message["content"])
                output = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                output = {"_unparsed": message["content"]}
        usage = data.get("usage") or {}
        return ModelResponse(output=output, raw=data, refused=bool(message.get("refusal")),
                             input_tokens=usage.get("prompt_tokens"),
                             output_tokens=usage.get("completion_tokens"),
                             model_build=data.get("model"))  # fmt: skip
