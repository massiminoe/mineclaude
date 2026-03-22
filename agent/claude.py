"""Anthropic SDK wrapper with tool definitions."""

from __future__ import annotations

from typing import Any

import anthropic

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
                    "description": "Python code to execute. Has access to all primitives (goToPosition, collectBlock, etc). Use await for async calls. Return a result string.",
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
        "name": "nearbyBlocks",
        "description": "Get blocks within range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "range": {"type": "integer", "description": "Search radius (default 16)", "default": 16}
            },
        },
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
        "name": "queueStatus",
        "description": "View running action, pending queue, and recent action history.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "queueClear",
        "description": "Cancel all pending actions in the queue.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "queueRemove",
        "description": "Cancel a specific pending action by ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Action ID to cancel"}
            },
            "required": ["id"],
        },
    },
    {
        "name": "stop",
        "description": "Emergency stop: clear the action queue and interrupt the running action.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class ClaudeClient:
    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None):
        self.model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    @observe(as_type="generation")
    async def send(
        self,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> anthropic.types.Message:
        return await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=TOOLS,
            messages=messages,
        )

    async def close(self) -> None:
        await self._client.close()
