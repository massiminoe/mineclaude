"""Provider registry + provider-aware ClaudeClient request shaping."""

from __future__ import annotations

import pytest

from agent.claude import TOOLS, ClaudeClient
from agent.providers import LLMProvider, resolve_provider


def _client(provider: LLMProvider) -> ClaudeClient:
    # Explicit api_key so AsyncAnthropic construction never touches env/network.
    return ClaudeClient(provider, api_key="test-key")


def test_default_provider_is_anthropic():
    p = resolve_provider(None)
    assert p.name == "anthropic"
    assert p.base_url is None
    assert p.supports_prompt_caching is True
    assert p.supports_thinking is False


def test_fireworks_kimi_provider_shape():
    p = resolve_provider("fireworks")
    assert p.model == "accounts/fireworks/models/kimi-k2p6"
    assert p.api_key_env == "FIREWORKS_API_KEY"
    # No /v1 suffix — the Anthropic SDK appends /v1/messages itself.
    assert p.base_url == "https://api.fireworks.ai/inference"
    assert p.supports_prompt_caching is False
    assert p.supports_vision is True
    assert p.supports_thinking is True
    assert p.thinking_budget_tokens == 1024
    assert p.default_temperature == 0.3


def test_openrouter_gemini_provider_shape():
    p = resolve_provider("openrouter")
    assert p.model == "google/gemini-3.5-flash"
    assert p.api_key_env == "OPENROUTER_API_KEY"
    # No /v1 suffix — the Anthropic SDK appends /v1/messages itself.
    assert p.base_url == "https://openrouter.ai/api"
    assert p.supports_prompt_caching is False
    assert p.supports_vision is True
    assert p.supports_thinking is True
    assert p.thinking_budget_tokens == 1024
    assert p.default_temperature == 0.3


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        resolve_provider("does-not-exist")


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("FIREWORKS_MODEL", "accounts/fireworks/models/kimi-k2p5")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-3-flash-preview")
    assert resolve_provider("anthropic").model == "claude-opus-4-8"
    assert resolve_provider("fireworks").model == "accounts/fireworks/models/kimi-k2p5"
    assert resolve_provider("openrouter").model == "google/gemini-3-flash-preview"


def test_anthropic_client_request_shaping():
    c = _client(resolve_provider("anthropic"))
    # Caching marker present on the system block.
    blocks = c._system_blocks("hi")
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    # No thinking, no temperature override (matches pre-change behaviour).
    assert c._sampling_kwargs(4096) == {}
    # Vision-capable -> screenshot tool retained.
    assert any(t["name"] == "screenshot" for t in c.tools())


def test_fireworks_client_request_shaping():
    c = _client(resolve_provider("fireworks"))
    # Fireworks auto-caches -> no marker (it rejects it on tool defs).
    assert "cache_control" not in c._system_blocks("hi")[0]
    # Thinking enabled, budget capped under max_tokens, temperature omitted.
    sk = c._sampling_kwargs(4096)
    assert sk == {"thinking": {"type": "enabled", "budget_tokens": 1024}}
    assert "temperature" not in sk
    # Compaction path skips thinking but keeps the sampling temperature.
    assert c._sampling_kwargs(2048, allow_thinking=False) == {"temperature": 0.3}
    # K2.6 is multimodal -> screenshot tool retained.
    assert any(t["name"] == "screenshot" for t in c.tools())


def test_openrouter_client_request_shaping():
    c = _client(resolve_provider("openrouter"))
    # Rely on OpenRouter/Gemini implicit caching -> no cache_control marker.
    assert "cache_control" not in c._system_blocks("hi")[0]
    # Thinking enabled at the small budget, temperature omitted while on.
    sk = c._sampling_kwargs(4096)
    assert sk == {"thinking": {"type": "enabled", "budget_tokens": 1024}}
    # Compaction path skips thinking but keeps the sampling temperature.
    assert c._sampling_kwargs(2048, allow_thinking=False) == {"temperature": 0.3}
    # Gemini 3.5 Flash is multimodal -> screenshot tool retained.
    assert any(t["name"] == "screenshot" for t in c.tools())
    # Routed at OpenRouter's Anthropic skin (SDK normalizes a trailing slash).
    assert str(c._client.base_url).rstrip("/") == "https://openrouter.ai/api"


def test_text_only_provider_drops_screenshot():
    text_only = LLMProvider(
        name="text-only",
        label="text only",
        model="m",
        api_key_env="X",
        supports_vision=False,
    )
    c = _client(text_only)
    names = {t["name"] for t in c.tools()}
    assert "screenshot" not in names
    # The rest of the tool set is untouched.
    assert names == {t["name"] for t in TOOLS} - {"screenshot"}


def test_thinking_budget_capped_below_max_tokens():
    c = _client(resolve_provider("fireworks"))
    sk = c._sampling_kwargs(512)
    assert sk["thinking"]["budget_tokens"] == min(1024, 512 - 256)


class _Block:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


def test_thinking_block_roundtrip():
    from agent.agent import Agent

    sig = Agent._thinking_block(_Block("thinking", thinking="reasoning...", signature="abc"))
    assert sig == {"type": "thinking", "thinking": "reasoning...", "signature": "abc"}

    # Signature omitted when absent (Fireworks may not return one).
    nosig = Agent._thinking_block(_Block("thinking", thinking="r", signature=None))
    assert nosig == {"type": "thinking", "thinking": "r"}

    redacted = Agent._thinking_block(_Block("redacted_thinking", data="enc"))
    assert redacted == {"type": "redacted_thinking", "data": "enc"}
