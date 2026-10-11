"""A provider backed by a Python function: simulated models for tests and for
evaluating the pipeline at scale without spending (design 05 §7)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from thisness.llm.types import ModelRequest, ModelResponse


class FunctionProvider:
    def __init__(self, answer: Callable[[ModelRequest], dict[str, Any] | None], *,
                 name: str = "simulated", local: bool = True) -> None:  # fmt: skip
        self.answer = answer
        self.name = name
        self.local = local

    def invoke(self, request: ModelRequest) -> ModelResponse:
        output = self.answer(request)
        text = request.system + "".join(m.content for m in request.messages)
        return ModelResponse(output=output, raw={"simulated": True}, refused=output is None,
                             input_tokens=len(text) // 4 + 1,
                             output_tokens=len(str(output)) // 4 + 1, model_build=self.name)  # fmt: skip
