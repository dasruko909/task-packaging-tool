"""Explicit prices and model-usage cost calculations."""

from __future__ import annotations

from dataclasses import dataclass

from .settings import MODELS, Settings, model_spec

# Short-context standard rates, derived from the shared model definition.
MODEL_PRICES = {name: spec.prices() for name, spec in MODELS.items()}


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0


def usage_cost(model: str, usage: TokenUsage) -> float:
    """Calculate cost from actual token counts returned by the API."""

    price = model_spec(model).prices(usage.input_tokens)
    uncached = max(0, usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens)
    return (
        uncached * price["input"]
        + usage.cached_input_tokens * price["cached_input"]
        + usage.cache_write_tokens * price["cache_write"]
        + usage.output_tokens * price["output"]
    ) / 1_000_000


def conservative_project_estimate(subtasks: int, task_type: str, model: str | None = None) -> tuple[int, int, float]:
    """Return explicit assumptions: input tokens, output tokens, and an estimated cost.

    This does not promise a PDF/image token count. The exact amount is known only
    after an API response and is stored in project state.
    """

    model = Settings.from_environment(model).model
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
    # These assumptions are per request. Summing input across requests must not
    # accidentally select the long-context rate for a large project.
    price = model_spec(model).prices(6_000)
    cost = (input_tokens * price['input'] + output_tokens * price['output']) / 1_000_000
    return input_tokens, output_tokens, cost
