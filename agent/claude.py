"""Anthropic SDK wrapper with tool definitions."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import anthropic

from agent.pricing import usage_to_dict
from agent.providers import EFFORT_RATIOS, LLMProvider, resolve_provider

UsageCallback = Callable[[str, dict[str, int]], Awaitable[None]]

try:
    from langfuse import observe
except ImportError:
    def observe(*args, **kwargs):
        if args and callable(args[0]):
            return args[0]
        def decorator(fn):
            return fn
        return decorator


TOOLS: list[dict[str, Any]] = [
    {
        "name": "newAction",
        "description": "Write Python code to perform actions in Minecraft. Use `await` for all async primitives. Must return a result string.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Has access to all primitives (goToPosition, breakBlockAt, findBlocks, etc). Use await for async calls. Return a result string.",
                }
            },
            "required": ["code"],
        },
    },
    {
        "name": "stats",
        "description": "Get current bot stats: health, hunger, position, biome, time of day.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "inventory",
        "description": "Get full inventory listing.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "nearbyEntities",
        "description": "Get entities within range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "range": {"type": "integer", "description": "Search radius (default 32)", "default": 32}
            },
        },
    },
    {
        "name": "screenshot",
        "description": (
            "Take a screenshot of the first-person view. Returns the image for visual analysis. "
            "Use when you need to see what's around you, verify a build, check terrain, or when "
            "text-based block data isn't sufficient.\n\n"
            "By default the camera points wherever the player happens to be facing (often arbitrary "
            "after Baritone movement). Aim it deliberately by passing either explicit yaw/pitch "
            "(MC convention: yaw 0=south, 90=west, 180=north, -90=east; pitch 0=horizon, -90=up, "
            "90=down) or `look_at` to point the eye at a world coord (often more intuitive — pass "
            "the position of a block or entity from gameState/nearby data). The two are mutually "
            "exclusive. The new rotation persists after the capture."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "yaw": {"type": "number", "description": "Yaw in degrees (optional)"},
                "pitch": {"type": "number", "description": "Pitch in degrees, clamped to [-90, 90] (optional)"},
                "look_at": {
                    "type": "array",
                    "description": "[x, y, z] world coord to aim the eye at. Mutually exclusive with yaw/pitch.",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                },
            },
        },
    },
    {
        "name": "writePlan",
        "description": (
            "Replace the contents of ./state/plan.md with the given content. "
            "Use this for multi-step goals to track your approach across turns. "
            "The plan is re-read from disk and injected into your context each turn "
            "inside <plan_document> tags. This tool does not edit in place — emit "
            "the full new file content each time. Pass an empty string to clear the plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "Full new content of plan.md. Markdown recommended, no schema "
                        "enforced. Empty string clears the plan."
                    ),
                }
            },
            "required": ["content"],
        },
    },
    {
        "name": "writeMemory",
        "description": (
            "Replace the contents of ./state/memory.md with the given content. "
            "Memory is durable knowledge that outlives the current goal — locations "
            "(base, mines, portals), hazards, and persistent rules. It is re-read from "
            "disk and injected into your context each turn inside <memory> tags. This "
            "tool does not edit in place — emit the full new file content each time. "
            "To remove an entry, omit it from the new content. Pass an empty string to "
            "wipe memory entirely (rarely correct)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "Full new content of memory.md (plain markdown — structure it "
                        "however you like). See the Memory section of the system prompt "
                        "for guidance on what to record."
                    ),
                }
            },
            "required": ["content"],
        },
    },
]


class ClaudeClient:
    """Anthropic-SDK client, pointed at whichever provider `provider` selects.

    Despite the name we talk to non-Anthropic providers too: Fireworks exposes
    an Anthropic-compatible Messages API, so the only per-provider differences
    are base_url/api_key/model plus a few request-shaping flags (cache markers,
    vision tool, thinking, sampling) carried on `provider`.
    """

    def __init__(self, provider: LLMProvider | None = None, *, api_key: str | None = None):
        self.provider = provider or resolve_provider(None)
        self.model = self.provider.model
        key = api_key if api_key is not None else self.provider.api_key()
        self._client = anthropic.AsyncAnthropic(api_key=key, base_url=self.provider.base_url)
        # Set by Agent so every API call's token usage flows back into running
        # totals + session log + monitor broadcast. Both send() and send_raw()
        # pass the actual model used (send_raw can override per-call).
        self.on_usage: UsageCallback | None = None

    def tools(self) -> list[dict[str, Any]]:
        """Tool set for this provider — drops `screenshot` for text-only models."""
        if self.provider.supports_vision:
            return TOOLS
        return [t for t in TOOLS if t["name"] != "screenshot"]

    def _system_blocks(self, system: str) -> list[dict[str, Any]]:
        block: dict[str, Any] = {"type": "text", "text": system}
        # Anthropic caches only what's marked; Fireworks auto-caches the longest
        # prefix and rejects the marker on tool defs, so we mark only when the
        # provider wants it.
        if self.provider.supports_prompt_caching:
            block["cache_control"] = {"type": "ephemeral"}
        return [block]

    def _sampling_kwargs(self, max_tokens: int, *, allow_thinking: bool = True) -> dict[str, Any]:
        """Thinking / temperature kwargs for this provider.

        Anthropic forbids `temperature` when extended thinking is on, so the two
        are mutually exclusive here. `allow_thinking=False` is used for one-off
        calls (compaction) where a reasoning budget would just eat output room.
        """
        p = self.provider
        budget = 0
        if allow_thinking and p.reasoning_effort:
            # Effort-based reasoning (GPT-5 / o-series via OpenRouter). OpenRouter's
            # /v1/messages skin DROPS a top-level reasoning:{effort} field (verified:
            # minimal vs high produced identical output, 0 thinking tokens), but it
            # DOES accept the Anthropic thinking block and translates a token budget
            # into an effort level for effort-only models. A budget ≈ ratio×max_tokens
            # lands in the matching bucket (OpenRouter's documented effort_ratio:
            # medium=0.5, high=0.8, …). So we express effort as a budget here.
            ratio = EFFORT_RATIOS.get(p.reasoning_effort, EFFORT_RATIOS["medium"])
            budget = round(ratio * max_tokens)
        elif allow_thinking and p.supports_thinking and p.thinking_budget_tokens > 0:
            budget = p.thinking_budget_tokens
        if budget > 0:
            # max_tokens must exceed the thinking budget; keep some output room.
            budget = min(budget, max(1, max_tokens - 256))
            return {"thinking": {"type": "enabled", "budget_tokens": budget}}
        if p.default_temperature is not None:
            return {"temperature": p.default_temperature}
        return {}

    async def _emit_usage(self, model: str, response: anthropic.types.Message) -> None:
        if self.on_usage is None:
            return
        try:
            await self.on_usage(model, usage_to_dict(getattr(response, "usage", None)))
        except Exception:
            # Usage tracking must never break the agent loop.
            pass

    @observe(as_type="generation")
    async def send(
        self,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> anthropic.types.Message:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=self._system_blocks(system),
            tools=self.tools(),
            messages=messages,
            **self._sampling_kwargs(max_tokens),
        )
        await self._emit_usage(self.model, response)
        return response

    @observe(as_type="generation")
    async def send_raw(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> anthropic.types.Message:
        """Generic Claude call without the agent's main system prompt or tool set.

        Used by compaction (and potentially other one-off Claude calls) where
        the main system prompt + full tool list would be wasted tokens. Pass
        `model` to override the client's default model for this call (e.g.
        compaction running on a cheaper model than the main loop).
        """
        used_model = model or self.model
        response = await self._client.messages.create(
            model=used_model,
            max_tokens=max_tokens,
            system=system,
            tools=tools or [],
            messages=messages,
            # Compaction is a summarization pass — skip the thinking budget so
            # the (smaller) max_tokens all goes to the summary, but keep the
            # provider's sampling temperature.
            **self._sampling_kwargs(max_tokens, allow_thinking=False),
        )
        await self._emit_usage(used_model, response)
        return response

    async def close(self) -> None:
        await self._client.close()
