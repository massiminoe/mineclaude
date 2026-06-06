"""LLM provider registry: model + endpoint + capability flags.

One enum-style key (``LLM_PROVIDER``) selects everything the client needs to
talk to a provider: the model id, which API-key env var to read, the base_url
(``None`` means Anthropic's own endpoint), and capability flags that gate the
few places where request shaping differs between providers.

We deliberately keep talking to every provider through the Anthropic SDK:
Fireworks exposes an Anthropic-compatible ``/v1/messages`` surface, so switching
providers is just a base_url + api_key + model swap. The flags below capture the
handful of spots where that compat surface diverges from Anthropic-native
behaviour (prompt-cache markers, the vision/screenshot tool, extended thinking,
sampling).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace


# OpenRouter's documented effort_ratio: it sizes a reasoning budget as
# ratio × max_tokens for an effort level. We invert it — given an effort, send a
# budget of ratio × max_tokens so OpenRouter buckets it back to that effort on
# effort-only models (GPT-5/o-series). Keys are the valid `reasoning_effort`s.
EFFORT_RATIOS: dict[str, float] = {
    "xhigh": 0.95,
    "high": 0.8,
    "medium": 0.5,
    "low": 0.2,
    "minimal": 0.1,
}


@dataclass(frozen=True)
class LLMProvider:
    name: str
    label: str
    model: str
    api_key_env: str
    base_url: str | None = None
    # Anthropic needs an explicit cache_control marker to cache; Fireworks
    # auto-caches the longest matching prefix and rejects the marker on tool
    # defs, so we omit it there.
    supports_prompt_caching: bool = False
    # Gate the screenshot tool + image blocks. False => text-only model: the
    # client drops the screenshot tool so the agent never asks for vision.
    supports_vision: bool = True
    # Extended thinking. thinking_budget_tokens drives "how much" when enabled.
    supports_thinking: bool = False
    thinking_budget_tokens: int = 0
    # Effort-based reasoning (OpenAI GPT-5 / o-series via OpenRouter). When set
    # (e.g. "medium"), the client expresses it as an Anthropic thinking budget of
    # EFFORT_RATIOS[effort] × max_tokens — OpenRouter's /v1/messages skin DROPS a
    # top-level reasoning:{effort} field but accepts the thinking block and
    # buckets the budget back to an effort level for effort-only models. Takes
    # precedence over thinking_budget_tokens. Valid keys: see EFFORT_RATIOS.
    reasoning_effort: str | None = None
    # Sampling. Anthropic forbids `temperature` when thinking is on, so the
    # client only applies this when thinking is off.
    default_temperature: float | None = None
    # Optional env var that overrides `model` (back-compat / pin a snapshot).
    model_env: str | None = None

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


_PROVIDERS: dict[str, LLMProvider] = {
    "anthropic": LLMProvider(
        name="anthropic",
        label="Claude (Anthropic)",
        model="claude-sonnet-4-6",
        model_env="CLAUDE_MODEL",
        api_key_env="ANTHROPIC_API_KEY",
        base_url=None,
        supports_prompt_caching=True,
        supports_vision=True,
        supports_thinking=False,
        default_temperature=None,
    ),
    "fireworks": LLMProvider(
        name="fireworks",
        label="Kimi K2.6 (Fireworks)",
        model="accounts/fireworks/models/kimi-k2p6",
        model_env="FIREWORKS_MODEL",
        api_key_env="FIREWORKS_API_KEY",
        # No /v1 suffix: the Anthropic SDK appends /v1/messages itself.
        base_url="https://api.fireworks.ai/inference",
        supports_prompt_caching=False,  # Fireworks auto-caches; no cache_control marker
        supports_vision=True,           # K2.6 is natively multimodal (MoonViT encoder)
        supports_thinking=True,
        thinking_budget_tokens=1024,    # "small thinking"
        default_temperature=0.3,        # only used if thinking is disabled
    ),
    "openrouter": LLMProvider(
        name="openrouter",
        label="Gemini 3.5 Flash (OpenRouter)",
        model="google/gemini-3.5-flash",
        model_env="OPENROUTER_MODEL",
        api_key_env="OPENROUTER_API_KEY",
        # OpenRouter's Anthropic "skin": /v1/messages in Anthropic format. No
        # /v1 suffix — the SDK appends it. Flags below are tuned for
        # gemini-3.5-flash; point OPENROUTER_MODEL at a non-multimodal model and
        # supports_vision would need revisiting.
        base_url="https://openrouter.ai/api",
        supports_prompt_caching=False,  # rely on OR/Gemini implicit caching
        supports_vision=True,           # Gemini 3.5 Flash is multimodal
        supports_thinking=True,
        thinking_budget_tokens=1024,    # "small thinking"
        default_temperature=0.3,        # only used if thinking is disabled
    ),
    "gpt5mini": LLMProvider(
        name="gpt5mini",
        label="GPT-5.4 Mini (OpenRouter, medium reasoning)",
        model="openai/gpt-5.4-mini",
        # Its OWN override env, NOT OPENROUTER_MODEL — that one belongs to the
        # gemini `openrouter` entry, and sharing it would clobber this model to
        # gemini whenever OPENROUTER_MODEL is set in the env.
        model_env="GPT5MINI_MODEL",
        api_key_env="OPENROUTER_API_KEY",
        # Same OpenRouter Anthropic skin as the `openrouter` entry — only the
        # model + reasoning control differ. No /v1 suffix; the SDK appends it.
        base_url="https://openrouter.ai/api",
        supports_prompt_caching=False,  # rely on OpenRouter/OpenAI implicit caching
        supports_vision=True,           # GPT-5.4 Mini takes image input
        supports_thinking=True,         # a reasoning model — but driven by effort, below
        thinking_budget_tokens=0,       # not a budget model; reasoning_effort drives it
        reasoning_effort="medium",      # OpenRouter unified reasoning effort
        default_temperature=None,       # reasoning models ignore temperature
    ),
}

DEFAULT_PROVIDER = "anthropic"


def provider_names() -> list[str]:
    return list(_PROVIDERS)


def resolve_provider(name: str | None) -> LLMProvider:
    """Resolve an LLM_PROVIDER key to its LLMProvider, applying env overrides.

    Raises ValueError on an unknown key (callers surface this and exit).
    """
    key = (name or DEFAULT_PROVIDER).strip()
    if key not in _PROVIDERS:
        valid = ", ".join(_PROVIDERS)
        raise ValueError(f"Unknown LLM_PROVIDER {key!r}; valid options: {valid}")
    provider = _PROVIDERS[key]
    # Per-provider model override (CLAUDE_MODEL / FIREWORKS_MODEL) keeps the
    # existing env knob working and lets you pin a specific model snapshot.
    if provider.model_env:
        override = os.environ.get(provider.model_env)
        if override:
            provider = replace(provider, model=override)
    # REASONING_EFFORT tunes effort-based providers (gpt5mini) without a code
    # edit — handy for trying low/high. Scoped to providers that already route
    # effort, so it can't accidentally flip on reasoning for a budget/no-think
    # provider.
    if provider.reasoning_effort:
        effort_override = os.environ.get("REASONING_EFFORT")
        if effort_override:
            provider = replace(provider, reasoning_effort=effort_override.strip())
    return provider
