from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from thisness.core import Actor, PairJudgment, Verdict
from thisness.llm import (
    OMIT,
    BudgetExhausted,
    LLMClient,
    LLMPolicy,
    ModelRequest,
    Prompt,
    ProviderError,
    filter_attributes,
    shown,
)
from thisness.llm.providers import FunctionProvider, RecordingProvider, ReplayProvider
from thisness.llm.providers.anthropic import AnthropicProvider
from thisness.llm.providers.openai_compatible import OpenAICompatibleProvider
from thisness.profiles.person import person
from thisness.storage.duckdb.invariants import check


class Verdict2(BaseModel):
    same: bool
    reason: str


PROMPT_TEXT = """=== system ===
You compare two records.
=== user ===
Records:
{payload}
"""


def prompt(text: str = PROMPT_TEXT, response: type[BaseModel] = Verdict2) -> Prompt:
    return Prompt.parse("compare", "1", text, response)


def client(store, ids, now, answer, **kwargs) -> LLMClient:
    provider = kwargs.pop("provider", None) or FunctionProvider(answer, name="fake")
    return LLMClient(store, provider, ids=ids, clock=lambda: now, **kwargs)


# -- prompts and privacy --------------------------------------------------------------------


def test_a_prompt_is_versioned_by_its_text_and_its_schema():
    class Other(BaseModel):
        same: bool

    base = prompt()
    assert base.ref == prompt().ref
    assert base.ref != prompt(PROMPT_TEXT.replace("compare", "contrast")).ref
    assert base.ref != prompt(response=Other).ref
    assert '"a": 1' in base.messages({"a": 1})[0].content
    with pytest.raises(ValueError, match="sections"):
        Prompt.parse("x", "1", "no sections", Verdict2)
    with pytest.raises(ValueError, match="placeholder"):
        Prompt.parse("x", "1", "=== system ===\ns\n=== user ===\nno payload", Verdict2)


def test_privacy_follows_each_attributes_policy():
    attributes = {"name": "Bob Smith", "email": ["b@x.com"], "date_of_birth": "1980-02-03",
                  "address_line": "1 Oak St", "identifiers": ["SSN:1"], "shoe_size": "9",
                  "city": ""}  # fmt: skip
    out, withheld = filter_attributes(attributes, person().attributes)
    assert out["name"] == "Bob Smith"
    assert out["email"][0].startswith("#") and out["date_of_birth"] == "[redacted]"
    assert set(withheld) == {"address_line", "identifiers", "shoe_size"}
    local, withheld_local = filter_attributes(attributes, person().attributes, local=True)
    assert local["address_line"] == "1 Oak St" and "address_line" not in withheld_local
    assert "identifiers" in withheld_local  # forbidden even locally
    assert shown("x", "forbidden") is OMIT and shown("x", "allow") == "x"


# -- the client -----------------------------------------------------------------------------


def test_a_call_is_validated_recorded_and_then_cached(store, ids, now):
    calls = []

    def answer(request):
        calls.append(request)
        return {"same": True, "reason": "one company"}

    c = client(store, ids, now, answer)
    first = c.call(prompt(), model="m1", payload={"left": "IBM", "right": "I.B.M."}, run_id=None)
    assert first.ok and first.parsed.same and first.invocation.status == "ok"
    stored = store.invocation(first.invocation.invocation_id)
    assert stored.payload == {"left": "IBM", "right": "I.B.M."} and stored.input_tokens > 0
    again = c.call(prompt(), model="m1", payload={"left": "IBM", "right": "I.B.M."})
    assert again.invocation.status == "cached" and len(calls) == 1
    assert again.invocation.cached_from == first.invocation.invocation_id
    # A different model, payload or prompt misses.
    c.call(prompt(), model="m2", payload={"left": "IBM", "right": "I.B.M."})
    c.call(prompt(), model="m1", payload={"left": "IBM", "right": "IBM Corp"})
    c.call(prompt(PROMPT_TEXT + "\nBe careful."), model="m1",
           payload={"left": "IBM", "right": "I.B.M."})  # fmt: skip
    assert len(calls) == 4
    assert len(store.invocations()) == 5
    assert check(store) == []


def test_an_invalid_answer_is_retried_once_and_never_coerced(store, ids, now):
    answers = iter([{"same": "maybe"}, {"same": True, "reason": "ok"}])
    fixed = client(store, ids, now, lambda r: next(answers)).call(prompt(), model="m",
                                                                   payload={"x": 1})  # fmt: skip
    assert fixed.ok and fixed.invocation.attempts == 2
    broken = client(store, ids, now, lambda r: {"same": "maybe"}).call(prompt(), model="m",
                                                                        payload={"x": 2})  # fmt: skip
    assert not broken.ok and broken.invocation.status == "invalid"
    assert broken.invocation.attempts == 2 and "same" in broken.invocation.error
    refused = client(store, ids, now, lambda r: None).call(prompt(), model="m", payload={"x": 3})
    assert refused.invocation.status == "refused" and refused.invocation.attempts == 1

    def fail(request):
        raise ProviderError("rate limited", retryable=True, raw={"status": 429})

    error = client(store, ids, now, fail, policy=LLMPolicy(backoff_seconds=0)).call(
        prompt(), model="m", payload={"x": 4})  # fmt: skip
    assert error.invocation.status == "error" and "rate limited" in error.invocation.error
    assert error.invocation.attempts == 4  # retried three times, then given up
    # Failures are never served from the cache.
    assert client(store, ids, now, lambda r: {"same": False, "reason": "no"}).call(
        prompt(), model="m", payload={"x": 2}).ok  # fmt: skip


def test_limits_stop_calls_before_anything_is_sent(store, ids, now):
    sent = []

    def answer(request):
        sent.append(1)
        return {"same": True, "reason": "r"}

    c = client(store, ids, now, answer, policy=LLMPolicy(max_calls=2))
    c.call(prompt(), model="m", payload={"x": 1})
    c.call(prompt(), model="m", payload={"x": 2})
    with pytest.raises(BudgetExhausted, match="2 calls"):
        c.call(prompt(), model="m", payload={"x": 3})
    assert len(sent) == 2
    unpriced = client(store, ids, now, answer, policy=LLMPolicy(budget_usd=1.0))
    with pytest.raises(BudgetExhausted, match="no price"):
        unpriced.call(prompt(), model="mystery-model", payload={"x": 4})
    priced = client(store, ids, now, answer, policy=LLMPolicy(budget_usd=0.01),
                    prices={"cheap": (1.0, 5.0)})  # fmt: skip
    priced.call(prompt(), model="cheap", payload={"x": 5}, max_output_tokens=100)
    assert 0 < priced.spent_usd < 0.01
    with pytest.raises(BudgetExhausted, match="could cost"):
        priced.call(prompt(), model="cheap", payload={"x": 6}, max_output_tokens=10_000)
    small = client(store, ids, now, answer, policy=LLMPolicy(max_input_tokens=10))
    with pytest.raises(BudgetExhausted, match="input tokens"):
        small.call(prompt(), model="m", payload={"x": "y" * 400})


# -- adapters ---------------------------------------------------------------------------------

REQUEST = ModelRequest(model="claude-sonnet-5", system="s",
                       messages=({"role": "user", "content": "hi"},), schema_name="answer",
                       response_schema=Verdict2.model_json_schema())  # fmt: skip


def test_anthropic_answers_by_forced_tool_use():
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="..."),
                     SimpleNamespace(type="tool_use", input={"same": True, "reason": "r"})],
            stop_reason="tool_use", model="claude-sonnet-5-20260901",
            usage=SimpleNamespace(input_tokens=120, output_tokens=30))  # fmt: skip

    provider = AnthropicProvider(client=SimpleNamespace(messages=SimpleNamespace(create=create)))
    response = provider.invoke(REQUEST)
    assert response.output == {"same": True, "reason": "r"} and not response.refused
    assert (response.input_tokens, response.output_tokens) == (120, 30)
    assert seen["tool_choice"] == {"type": "tool", "name": "answer"}
    assert "temperature" not in seen  # left to the provider unless set

    class RateLimited(Exception):
        status_code = 429

    def limited(**kwargs):
        raise RateLimited("slow down")

    with pytest.raises(ProviderError) as info:
        AnthropicProvider(client=SimpleNamespace(messages=SimpleNamespace(create=limited))) \
            .invoke(REQUEST)  # fmt: skip
    assert info.value.retryable


def test_openai_compatible_checks_status_before_the_body():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["model"] == "limited":
            return httpx.Response(429, text="rate limited")
        if body["model"] == "empty":
            return httpx.Response(200, json={"choices": []})
        assert body["response_format"]["type"] == "json_schema"
        assert body["messages"][0] == {"role": "system", "content": "s"}
        content = json.dumps({"same": False, "reason": "different cities"})
        return httpx.Response(200, json={"model": "llama-3", "choices": [
            {"message": {"content": content}}], "usage": {"prompt_tokens": 50,
                                                          "completion_tokens": 9}})  # fmt: skip

    provider = OpenAICompatibleProvider("http://gpu.local/v1", local=True,
                                        client=httpx.Client(transport=httpx.MockTransport(handler)))  # fmt: skip
    response = provider.invoke(REQUEST)
    assert response.output == {"same": False, "reason": "different cities"}
    assert response.input_tokens == 50 and provider.local
    for model in ("limited", "empty"):
        with pytest.raises(ProviderError) as info:
            provider.invoke(REQUEST.model_copy(update={"model": model}))
        assert info.value.retryable


def test_openai_takes_its_own_token_limit_and_reports_cut_off_answers():
    import httpx

    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body["model"] == "thinker":  # all its tokens spent reasoning
            return httpx.Response(200, json={"choices": [
                {"message": {"content": ""}, "finish_reason": "length"}]})  # fmt: skip
        return httpx.Response(200, json={"choices": [
            {"message": {"content": json.dumps({"same": True, "reason": "x"})}}]})  # fmt: skip

    transport = httpx.Client(transport=httpx.MockTransport(handler))
    openai = OpenAICompatibleProvider("https://api.openai.com/v1", api_key="k", client=transport)
    openai.invoke(REQUEST)
    assert bodies[-1]["max_completion_tokens"] == REQUEST.max_output_tokens
    assert "max_tokens" not in bodies[-1]
    local = OpenAICompatibleProvider("http://gpu.local/v1", local=True, client=transport)
    local.invoke(REQUEST)
    assert bodies[-1]["max_tokens"] == REQUEST.max_output_tokens
    with pytest.raises(ProviderError, match="cut off") as info:
        openai.invoke(REQUEST.model_copy(update={"model": "thinker"}))
    assert not info.value.retryable


def test_recorded_responses_replay_offline(store, ids, now, tmp_path):
    cassette = tmp_path / "calls.jsonl"
    live = RecordingProvider(FunctionProvider(lambda r: {"same": True, "reason": "x"},
                                              name="anthropic"), cassette)  # fmt: skip
    LLMClient(store, live, ids=ids, clock=lambda: now).call(prompt(), model="m", payload={"x": 1})
    replay = ReplayProvider(cassette, name="anthropic")
    fresh = LLMClient(store, replay, ids=ids, clock=lambda: now)
    assert fresh.call(prompt(), model="m", payload={"x": 1}, use_cache=False).parsed.same
    missing = fresh.call(prompt(), model="m", payload={"x": 2})
    assert missing.invocation.status == "error" and "re-record" in missing.invocation.error


# -- records ---------------------------------------------------------------------------------


def test_decisions_cite_recorded_invocations(store, ids, now, world):
    a, b = world.obs("IBM"), world.obs("I.B.M.")
    answer = client(store, ids, now, lambda r: {"same": True, "reason": "r"}).call(
        prompt(), model="m", payload={"x": 1})  # fmt: skip
    judgment = PairJudgment(decision_id=ids.decision(), actor=Actor(kind="agent", id="m"),
                            left_id=min(a, b), right_id=max(a, b), verdict=Verdict.MATCH,
                            invocation_id=answer.invocation.invocation_id, created_at=now)  # fmt: skip
    store.record_decisions([judgment])
    assert check(store) == []
    from thisness.engine import Thisness

    explained = Thisness(store, ids=ids, clock=lambda: now).explain(judgment.decision_id)
    assert explained["invocation"]["output"] == {"same": True, "reason": "r"}
    orphan = judgment.model_copy(update={"decision_id": ids.decision(),
                                         "invocation_id": ids.invocation()})  # fmt: skip
    store.record_decisions([orphan])
    assert [v.check for v in check(store)] == ["every cited model invocation is recorded"]
    store._con.execute("DELETE FROM decisions WHERE decision_id = ?", [orphan.decision_id])


def test_a_dropped_connection_is_retried_and_a_permanent_error_is_not(store, ids, now):
    """65 of 625 calls failed on dropped connections and timeouts at 16 in flight on the
    first real run (design 07 §13.3); one is now retried, with backoff."""
    outcomes = iter([ProviderError("Server disconnected", retryable=True),
                     ProviderError("ReadTimeout", retryable=True),
                     {"same": True, "reason": "ok"}])  # fmt: skip

    def flaky(request):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    fast = LLMPolicy(backoff_seconds=0)
    recovered = client(store, ids, now, flaky, policy=fast).call(prompt(), model="m",
                                                                 payload={"x": 9})  # fmt: skip
    assert recovered.ok and recovered.invocation.attempts == 3
    sent = []

    def forbidden(request):
        sent.append(1)
        raise ProviderError("HTTP 400: bad request", retryable=False)

    refused = client(store, ids, now, forbidden, policy=fast).call(prompt(), model="m",
                                                                    payload={"x": 10})  # fmt: skip
    assert refused.invocation.status == "error" and len(sent) == 1
