"""Approximate USD cost of a run from its token usage.

Prices are USD per million tokens: (input, output, cache_read). Approximate list prices, update as needed.
- claude-opus-5-5: Anthropic first-party API list price (checked 2026-10). Cache *writes* (1.25x input)
  are not tracked by worker.llm.Usage, so Claude costs are a slight underestimate.
- gpt-5.4: unverified placeholder - check https://openai.com/api/pricing before quoting.
- Gemini free-tier models cost $0 within the free quota.
"""
from __future__ import annotations

PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "gpt-5.4": (2.50, 15.00, 0.25),
}
FREE_TIER = {"gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.5-flash"}


def cost_usd(model: str, usage: dict, provider: str = "") -> float | None:
    """None when the model's price is unknown. Anthropic reports input_tokens *excluding* cache reads;
    OpenAI-protocol providers include cached tokens in prompt_tokens, so they are subtracted there."""
    if model in FREE_TIER:
        return 0.0
    if model not in PRICES:
        return None
    p_in, p_out, p_cache = PRICES[model]
    cached = usage.get("cache_read_tokens", 0)
    uncached = usage.get("input_tokens", 0) - (0 if provider == "anthropic" else cached)
    return (max(uncached, 0) * p_in + usage.get("output_tokens", 0) * p_out + cached * p_cache) / 1e6


def fmt_cost(model: str, cost: float | None) -> str:
    if cost is None:
        return "n/a"
    return "$0 (free tier)" if model in FREE_TIER else f"${cost:.3f}"
