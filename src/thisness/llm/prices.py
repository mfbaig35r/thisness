"""List prices, in US dollars per million tokens (input, output).

For cost reporting and run budgets (design 05 §3.6). Checked 2026-10-07 against
published list prices; pass your own table to ``LLMClient`` for negotiated rates or
other models. A run with a dollar budget refuses a model that has no price here,
rather than guess.
"""

from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    # OpenAI standard (not batch or priority) list prices, checked 2026-10-08.
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6.1-sol": (2.0, 10.0),
}


def cost(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    prices: dict[str, tuple[float, float]] | None = None,
) -> float | None:
    table = PRICES if prices is None else prices
    if model not in table or input_tokens is None or output_tokens is None:
        return None
    per_in, per_out = table[model]
    return (input_tokens * per_in + output_tokens * per_out) / 1_000_000
