"""Per-model token pricing and usage helpers.

Rates are USD per 1M tokens for the standard 5-minute ephemeral cache. If a
model isn't in the table we fall back to Sonnet rates and log once — we'd
rather under-report than crash on an unrecognized model.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# (input, output, cache_write_5m, cache_read) per 1M tokens, USD.
# Matched by substring against the model id (e.g. "kimi" hits
# "accounts/fireworks/models/kimi-k2p6"). Fireworks bills no separate
# cache-write fee — caching is automatic — so cache_write is 0 for kimi.
_RATES: dict[str, tuple[float, float, float, float]] = {
    "opus":   (15.00, 75.00, 18.75, 1.50),
    "sonnet": (3.00,  15.00, 3.75,  0.30),
    "haiku":  (1.00,  5.00,  1.25,  0.10),
    "kimi":   (0.95,  4.00,  0.00,  0.16),
    # OpenRouter passes Gemini pricing through at cost (no per-token markup);
    # the only OR fee is the ~5.5% credit-purchase surcharge, which isn't a
    # per-call cost so it's not modelled here. Thinking tokens bill as output.
    "gemini": (1.50,  9.00,  0.00,  0.15),
}

_FALLBACK = _RATES["sonnet"]
_warned: set[str] = set()


def _rate_for(model: str) -> tuple[float, float, float, float]:
    m = (model or "").lower()
    for key, rate in _RATES.items():
        if key in m:
            return rate
    if model not in _warned:
        _warned.add(model)
        logger.warning(f"pricing: no rate for model {model!r}, falling back to sonnet")
    return _FALLBACK


def usage_to_dict(usage: Any) -> dict[str, int]:
    """Coerce an Anthropic Usage object (or dict) to plain ints.

    Missing / None fields become 0 so downstream math is total-safe.
    """
    def g(name: str) -> int:
        if isinstance(usage, dict):
            v = usage.get(name)
        else:
            v = getattr(usage, name, None)
        return int(v) if isinstance(v, (int, float)) else 0

    return {
        "input_tokens": g("input_tokens"),
        "output_tokens": g("output_tokens"),
        "cache_creation_input_tokens": g("cache_creation_input_tokens"),
        "cache_read_input_tokens": g("cache_read_input_tokens"),
    }


def compute_cost(model: str, usage: dict[str, int]) -> float:
    """USD cost of a single API call."""
    rate_in, rate_out, rate_write, rate_read = _rate_for(model)
    return (
        usage.get("input_tokens", 0) * rate_in
        + usage.get("output_tokens", 0) * rate_out
        + usage.get("cache_creation_input_tokens", 0) * rate_write
        + usage.get("cache_read_input_tokens", 0) * rate_read
    ) / 1_000_000.0


def empty_totals() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": 0.0,
        "calls": 0,
        "by_model": {},
    }


def accumulate(totals: dict[str, Any], model: str, usage: dict[str, int]) -> dict[str, Any]:
    """Mutate `totals` in place; return it for convenience.

    `totals` is shaped like `empty_totals()`. Per-model breakdown lives under
    `by_model[model]` with the same shape (minus `by_model` itself).
    """
    cost = compute_cost(model, usage)
    totals["calls"] += 1
    totals["cost_usd"] += cost
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        totals[k] += usage.get(k, 0)

    by_model = totals.setdefault("by_model", {})
    bucket = by_model.setdefault(model, {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": 0.0,
        "calls": 0,
    })
    bucket["calls"] += 1
    bucket["cost_usd"] += cost
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        bucket[k] += usage.get(k, 0)
    return totals
