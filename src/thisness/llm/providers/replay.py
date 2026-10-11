"""Recorded responses (design 05 §3.3, §7).

``RecordingProvider`` wraps a real provider and appends every exchange to a JSON
Lines cassette. ``ReplayProvider`` answers from a cassette and fails on a request it
has not seen, so tests run offline, free, and only against real responses.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from thisness._canonical import canonical_json
from thisness.llm.types import ModelProvider, ModelRequest, ModelResponse, ProviderError


def request_key(request: ModelRequest) -> str:
    return hashlib.sha256(canonical_json(request.model_dump(mode="json")).encode()).hexdigest()


class RecordingProvider:
    def __init__(self, inner: ModelProvider, cassette: str | Path) -> None:
        self.inner = inner
        self.name = inner.name
        self.local = inner.local
        self.path = Path(cassette)
        self._lock = threading.Lock()

    def invoke(self, request: ModelRequest) -> ModelResponse:
        response = self.inner.invoke(request)
        line = json.dumps({"key": request_key(request), "model": request.model,
                           "response": response.model_dump(mode="json")}, sort_keys=True)  # fmt: skip
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return response


class ReplayProvider:
    def __init__(self, cassette: str | Path, *, name: str, local: bool = False) -> None:
        self.name = name
        self.local = local
        self.responses: dict[str, ModelResponse] = {}
        for line in Path(cassette).read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                self.responses[entry["key"]] = ModelResponse.model_validate(entry["response"])

    def invoke(self, request: ModelRequest) -> ModelResponse:
        response = self.responses.get(request_key(request))
        if response is None:
            raise ProviderError(f"no recorded response for this {request.model} request; "
                                "re-record the cassette")  # fmt: skip
        return response
