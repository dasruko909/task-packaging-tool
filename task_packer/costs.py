"""Explicit prices and model-usage cost calculations."""

from __future__ import annotations

from dataclasses import dataclass


MAX_PROJECT_COST_USD = 5.00

# USD per million tokens. Values are intentionally kept in code so the estimate
# is auditable; the README gives the date and price source.
MODEL_PRICES = {
    "gpt-6-astra": {"input": 10.00, "cached_input": 1.00, "output": 50.00},
}


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


def usage_cost(model: str, usage: TokenUsage) -> float:
    """Calculate cost from actual token counts returned by the API."""

    if model not in MODEL_PRICES:
        raise RuntimeError(
            f"No price is configured for model {model!r}. Use gpt-6-astra or extend MODEL_PRICES."
        )
    price = MODEL_PRICES[model]
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens)
    return (
        uncached * price["input"]
        + usage.cached_input_tokens * price["cached_input"]
        + usage.output_tokens * price["output"]
    ) / 1_000_000


def conservative_project_estimate(subtasks: int, task_type: str, model: str = 'gpt-6-astra') -> tuple[int, int, float]:
    """Return explicit assumptions: input tokens, output tokens, and an estimated cost.

    This does not promise a PDF/image token count. The exact amount is known only
    after an API response and is stored in project state.
    """

    calls = 6 + subtasks * 4 + (1 if task_type == "multiple" else 0)
    if task_type == "auto":
        calls += 1
    input_tokens = calls * 6_000
    # Sum of pipeline output limits: statement 8k, generator 6k,
    # edge tests 6k, solution 7k, review 2.5k, editorial 6k.
    output_tokens = 43_000 + subtasks * 21_500
    if task_type != "standard":
        output_tokens += 8_000
    if task_type == 'interactive':
        output_tokens -= 5_000
    if task_type == "auto":
        output_tokens += 3_000
    cost = usage_cost(
        model,
        TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )
    return input_tokens, output_tokens, cost
