"""Calling a model the one way Thisness does (design 05 §3).

``LLMClient.call`` renders a prompt with a payload that is already filtered for
privacy, checks the run's limits, serves a cached answer when nothing that matters
has changed, invokes the provider, validates the answer against the prompt's
response model (retrying once on an invalid answer, never coercing one), prices the
call, and records it. Every call, successful or not, leaves a ``ModelInvocation``.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from thisness._canonical import canonical_json
from thisness.core.invocation import InvocationStatus, ModelInvocation
from thisness.ids import IdFactory
from thisness.llm.prices import cost
from thisness.llm.prompts import Prompt
from thisness.llm.types import ModelProvider, ModelRequest, ProviderError


class BudgetExhausted(Exception):
    """The run's call or spending limit would be exceeded. Nothing was sent."""


# The backoff's sleep, one name for tests to replace.
_sleep = time.sleep


@dataclass(frozen=True)
class LLMPolicy:
    """Limits for one client, usually one run (design 05 §3.6, PRD appendix A.12)."""

    max_calls: int | None = None
    max_input_tokens: int = 60_000  # per call, estimated before sending
    budget_usd: float | None = None
    concurrency: int = 4
    retries_on_invalid: int = 1
    # A dropped connection or a timeout is retried after 1, 2, 4 seconds by default; an
    # error the provider marks permanent is not. 65 of 625 calls failed this way on the
    # first real run at 16 in flight (design 07 §13.3).
    retries_on_transient: int = 3
    backoff_seconds: float = 1.0


@dataclass(frozen=True)
class Answer[T: BaseModel]:
    invocation: ModelInvocation
    parsed: T | None

    @property
    def ok(self) -> bool:
        return self.parsed is not None


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


class LLMClient:
    def __init__(
        self,
        store: Any,
        provider: ModelProvider,
        *,
        ids: IdFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        policy: LLMPolicy | None = None,
        prices: Mapping[str, tuple[float, float]] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.ids = ids or IdFactory()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.policy = policy or LLMPolicy()
        self.prices = dict(prices) if prices is not None else None
        self._lock = threading.Lock()
        self._registered: set[str] = set()
        self.calls = 0
        self.spent_usd = 0.0
        self._pending_usd = 0.0

    # -- keys and limits ---------------------------------------------------------------------

    def cache_key(self, prompt: Prompt, model: str, payload: Mapping[str, Any],
                  settings: Mapping[str, Any]) -> str:  # fmt: skip
        material = {"provider": self.provider.name, "model": model, "prompt": prompt.ref.ref,
                    "settings": dict(settings), "payload": dict(payload)}  # fmt: skip
        return hashlib.sha256(canonical_json(material).encode()).hexdigest()

    def _price(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        return cost(model, input_tokens, output_tokens, self.prices)

    def _reserve(self, model: str, input_tokens: int, output_tokens: int) -> float:
        policy = self.policy
        if input_tokens > policy.max_input_tokens:
            raise BudgetExhausted(
                f"about {input_tokens} input tokens; the limit is {policy.max_input_tokens}"
            )
        worst = self._price(model, input_tokens, output_tokens)
        with self._lock:
            if policy.max_calls is not None and self.calls >= policy.max_calls:
                raise BudgetExhausted(f"{self.calls} calls made; the limit is {policy.max_calls}")
            if policy.budget_usd is not None:
                if worst is None:
                    raise BudgetExhausted(
                        f"no price for {model}; a run with a budget cannot use it"
                    )
                if self.spent_usd + self._pending_usd + worst > policy.budget_usd:
                    raise BudgetExhausted(
                        f"${self.spent_usd:.4f} spent of ${policy.budget_usd:.2f}; this call "
                        f"could cost ${worst:.4f}"
                    )
            self.calls += 1
            self._pending_usd += worst or 0.0
        return worst or 0.0

    def _settle(self, reserved: float, actual: float | None) -> None:
        with self._lock:
            self._pending_usd -= reserved
            self.spent_usd += actual or 0.0

    # -- the call -------------------------------------------------------------------------------

    def call(
        self,
        prompt: Prompt,
        *,
        model: str,
        payload: Mapping[str, Any],
        run_id: str | None = None,
        stage_id: str | None = None,
        max_output_tokens: int = 4096,
        temperature: float | None = None,
        use_cache: bool = True,
    ) -> Answer:
        if prompt.ref.ref not in self._registered:
            self.store.register_component(prompt.ref, {"name": prompt.name,
                                                       "version": prompt.version})  # fmt: skip
            self._registered.add(prompt.ref.ref)
        settings = {"max_output_tokens": max_output_tokens, "temperature": temperature}
        key = self.cache_key(prompt, model, payload, settings)
        common = {"provider": self.provider.name, "model": model, "prompt_ref": prompt.ref.ref,
                  "settings": settings, "cache_key": key, "payload": dict(payload),
                  "run_id": run_id, "stage_id": stage_id}  # fmt: skip

        if use_cache and (hit := self.store.cached_invocation(key)) is not None:
            record = ModelInvocation(invocation_id=self.ids.invocation(), status="cached",
                                     output=hit.output, cached_from=hit.invocation_id,
                                     model_build=hit.model_build, attempts=0, cost_usd=0.0,
                                     latency_ms=0, created_at=self.clock(), **common)  # fmt: skip
            self.store.record_invocation(record)
            return Answer(record, prompt.response.model_validate(hit.output))

        request = ModelRequest(model=model, system=prompt.system,
                               messages=prompt.messages(dict(payload)),
                               schema_name=prompt.name.replace(".", "_"),
                               response_schema=prompt.schema,
                               max_output_tokens=max_output_tokens,
                               temperature=temperature)  # fmt: skip
        estimate = _estimate_tokens(request.system + "".join(m.content for m in request.messages))
        reserved = self._reserve(model, estimate, max_output_tokens)

        status: InvocationStatus = "error"
        parsed = output = raw = error = build = None
        tokens_in = tokens_out = 0
        attempts = 0
        started = time.perf_counter()
        invalid_left = self.policy.retries_on_invalid
        transient_left = self.policy.retries_on_transient
        while True:
            attempts += 1
            try:
                response = self.provider.invoke(request)
            except ProviderError as exc:
                status, error, raw = "error", str(exc), exc.raw
                if exc.retryable and transient_left > 0:
                    retry = self.policy.retries_on_transient - transient_left
                    transient_left -= 1
                    delay = self.policy.backoff_seconds * 2**retry
                    if delay > 0:
                        _sleep(delay)
                    continue
                break
            raw, build = response.raw, response.model_build
            tokens_in += response.input_tokens or 0
            tokens_out += response.output_tokens or 0
            if response.refused:
                status, error = "refused", "the model declined to answer"
                break
            try:
                parsed = prompt.response.model_validate(response.output)
            except ValidationError as exc:
                status, error = "invalid", str(exc)[:4000]
                if invalid_left > 0:
                    invalid_left -= 1
                    continue
                break
            status, output, error = "ok", parsed.model_dump(mode="json"), None
            break
        latency = int((time.perf_counter() - started) * 1000)
        actual = self._price(model, tokens_in, tokens_out) if attempts else None
        self._settle(reserved, actual)
        record = ModelInvocation(
            invocation_id=self.ids.invocation(), status=status, output=output, error=error,
            response=raw, model_build=build, attempts=attempts,
            input_tokens=tokens_in or None, output_tokens=tokens_out or None,
            latency_ms=latency, cost_usd=actual, created_at=self.clock(), **common,
        )  # fmt: skip
        self.store.record_invocation(record)
        return Answer(record, parsed if status == "ok" else None)
