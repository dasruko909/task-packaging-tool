"""Supported public Responses API models and environment settings.

Standard USD/1M token rates and capabilities verified on 2026-10-10:
https://developers.openai.com/api/docs/models/gpt-6-astra
https://developers.openai.com/api/docs/models/gpt-6.1-sol
https://developers.openai.com/api/docs/models/gpt-6-luna
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os


DEFAULT_MODEL = "gpt-6-astra"
API_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    input_price: float
    cached_input_price: float
    cache_write_price: float
    output_price: float
    context_window: int = 1_050_000
    max_output_tokens: int = 128_000
    long_context_threshold: int = 272_000
    reasoning_effort: str = "low"
    image_input: bool = True
    structured_output: bool = True

    def prices(self, input_tokens: int = 0) -> dict[str, float]:
        long = input_tokens > self.long_context_threshold
        return {
            "input": self.input_price * (2 if long else 1),
            "cached_input": self.cached_input_price * (2 if long else 1),
            "cache_write": self.cache_write_price * (2 if long else 1),
            "output": self.output_price * (1.5 if long else 1),
        }


MODELS = {
    "gpt-6-astra": ModelSpec(10.00, 1.00, 12.50, 50.00),
    "gpt-6.1-sol": ModelSpec(2.00, 0.10, 2.50, 10.00),
    "gpt-6-luna": ModelSpec(0.10, 0.01, 0.125, 0.50),
}


def model_spec(model: str) -> ModelSpec:
    if model not in MODELS:
        raise ValueError(
            f"Unsupported OPENAI_MODEL {model!r}. Choose: {', '.join(MODELS)}."
        )
    return MODELS[model]


@dataclass(frozen=True, slots=True)
class Settings:
    model: str
    budget_usd: float | None

    @classmethod
    def from_environment(cls, model: str | None = None) -> "Settings":
        selected = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL) if model is None else model
        model_spec(selected)
        raw_budget = os.environ.get("PACKER_BUDGET_USD", "").strip()
        budget = None
        if raw_budget:
            try:
                budget = float(raw_budget)
            except ValueError as error:
                raise ValueError("PACKER_BUDGET_USD must be a finite positive USD amount, or empty for no cap.") from error
            if not math.isfinite(budget) or budget <= 0:
                raise ValueError("PACKER_BUDGET_USD must be a finite positive USD amount, or empty for no cap.")
        return cls(selected, budget)
