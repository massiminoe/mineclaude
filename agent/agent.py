"""Core agent loop: chat → Claude → tool_use → execute → respond."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable, Coroutine

from agent.action_queue import ActionQueue
from agent.bridge import BridgeClient
from agent.claude import ClaudeClient
from agent.primitives import make_primitives
from agent.prompt import build_system_prompt, format_game_state
from agent.sandbox import SandboxError, execute

try:
    from langfuse import observe, propagate_attributes
    _langfuse_available = True
except ImportError:
    _langfuse_available = False
    propagate_attributes = None
    def observe(*args, **kwargs):
        """No-op decorator when langfuse is not installed."""
        if args and callable(args[0]):
            return args[0]
        def decorator(fn):
            return fn
        return decorator

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 10
CHAT_MAX_LEN = 240
LOG_TRIM = 4096


class Agent:
    def __init__(
        self,
        bridge: BridgeClient,
        claude: ClaudeClient,
        bot_name: str = "Mineclaw",
    ):
        self.bridge = bridge
        self.claude = claude
        self.bot_name = bot_name
        self.system_prompt = build_system_prompt(bot_name)
        self.messages: list[dict[str, Any]] = []
        self.queue = ActionQueue()
        self.primitives = make_primitives(bridge, on_subaction=self._on_subaction)
        self._session_ids: dict[str, str] = {}
        self._callbacks: dict[str, list[Callable[[str, Any], Coroutine[Any, Any, None]]]] = {}

        # Wire up executor and logging
        self.queue.set_executor(self._execute_action)
        self.queue.on("action:started", self._on_action_started)
        self.queue.on("action:completed", self._on_action_completed)

    def on(self, event: str, callback: Callable[[str, Any], Coroutine[Any, Any, None]]) -> None:
        self._callbacks.setdefault(event, []).append(callback)

    async def _emit(self, event: str, data: Any = None) -> None:
        for cb in self._callbacks.get(event, []):
            try:
                await cb(event, data)
            except Exception:
                pass

    async def start(self) -> None:
        """Start the agent: queue worker + event listener."""
        logger.info(f"Starting agent as {self.bot_name}")
        self.queue.start()
        await self.bridge.events(self._handle_event)

    async def _handle_event(self, event: dict) -> None:
        if event.get("type") == "chat":
            await self._handle_chat(event["data"])
        elif event.get("type") == "death":
            await self._handle_death()
        elif event.get("type") == "respawn":
            await self._handle_respawn()

    async def _handle_death(self) -> None:
        """Handle player death: stop all actions."""
        logger.warning("Bot died! Clearing action queue.")
        await self.queue.interrupt()
        try:
            await self.bridge.stop()
        except Exception:
            pass

    async def _handle_respawn(self) -> None:
        """Handle respawn: notify in chat."""
        logger.info("Bot respawned.")
        await self._send_chat("I died and respawned! Give me a moment to get my bearings.")

    async def _handle_chat(self, data: dict) -> None:
        username = data.get("username", "")

        # Ignore own messages
        if username.lower() == self.bot_name.lower():
            return

        if _langfuse_available and propagate_attributes:
            session_id = self._session_ids.setdefault(username, str(uuid.uuid4()))
            with propagate_attributes(user_id=username, session_id=session_id):
                await self._handle_chat_traced(data)
        else:
            await self._handle_chat_traced(data)

    @observe()
    async def _handle_chat_traced(self, data: dict) -> None:
        username = data.get("username", "")
        message = data.get("message", "")

        logger.info(f"Chat from {username}: {message}")

        # Fetch game state
        status_resp = await self.bridge.get_status()
        queue_status = self.queue.status()
        game_state = format_game_state(status_resp.data, queue_status)

        # Append user message
        self.messages.append({
            "role": "user",
            "content": f"{username}: {message}",
        })

        # Inject synthetic gameState tool_use/tool_result pair
        self.messages.append({
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "gamestate_auto",
                    "name": "gameState",
                    "input": {},
                }
            ],
        })
        self.messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "gamestate_auto",
                    "content": game_state,
                }
            ],
        })

        await self._emit("conversation:update", self.messages)

        # Claude loop
        for iteration in range(MAX_ITERATIONS):
            logger.info(f"Claude iteration {iteration + 1}/{MAX_ITERATIONS}")

            try:
                response = await self.claude.send(self.system_prompt, self.messages)
            except Exception as e:
                logger.error(f"Claude API error: {e}")
                await self._send_chat("Sorry, I had a brain glitch. Try again?")
                break

            logger.info(f"Claude response: stop_reason={response.stop_reason}, blocks={len(response.content)}")

            # Process response content
            assistant_content = []
            tool_uses = []
            text_parts = []

            for block in response.content:
                if block.type == "text":
                    logger.info(f"Claude text: {block.text[:LOG_TRIM]}")
                    text_parts.append(block.text)
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    if "code" in block.input:
                        other = {k: v for k, v in block.input.items() if k != "code"}
                        extra = f" {json.dumps(other)}" if other else ""
                        logger.info(f"Claude tool_use: {block.name}{extra}\n{block.input['code'][:LOG_TRIM]}")
                    else:
                        logger.info(f"Claude tool_use: {block.name}({json.dumps(block.input, indent=2)[:LOG_TRIM]})")
                    tool_uses.append(block)
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            self.messages.append({"role": "assistant", "content": assistant_content})
            await self._emit("conversation:update", self.messages)

            if tool_uses:
                # Dispatch all tool calls and collect results
                tool_results = []
                for tool_use in tool_uses:
                    result = await self._dispatch_tool(tool_use.name, tool_use.input)
                    logger.info(f"Tool result ({tool_use.name}): {result[:LOG_TRIM]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result,
                    })
                self.messages.append({"role": "user", "content": tool_results})
                await self._emit("conversation:update", self.messages)
                # Continue loop for Claude to process results
                continue

            # end_turn with text — send as chat
            if text_parts:
                full_text = " ".join(text_parts)
                logger.info(f"Sending chat: {full_text[:LOG_TRIM]}")
                await self._send_chat(full_text)
            break

        # Trim conversation history to avoid unbounded growth
        self._trim_history()

    @observe()
    async def _dispatch_tool(self, name: str, input_data: dict) -> str:
        """Route tool calls to appropriate handlers."""
        logger.debug(f"Tool call: {name}({json.dumps(input_data)[:LOG_TRIM]})")

        try:
            if name == "newAction":
                code = input_data.get("code", "")
                action = await self.queue.enqueue(code)
                await self.queue.drain()
                if action.status.value == "completed":
                    return action.result or "Action completed (no output)"
                else:
                    return f"Action failed: {action.error or 'unknown error'}"

            elif name == "stats":
                resp = await self.bridge.get_status()
                return json.dumps(resp.data, indent=2)

            elif name == "inventory":
                resp = await self.bridge.get_status()
                return json.dumps(resp.data.get("inventory", []), indent=2)

            elif name == "nearbyBlocks":
                radius = input_data.get("range", 16)
                resp = await self.bridge.get_nearby_blocks(radius)
                return json.dumps(resp.data.get("blocks", []), indent=2)

            elif name == "nearbyEntities":
                radius = input_data.get("range", 32)
                resp = await self.bridge.get_nearby_entities(radius)
                return json.dumps(resp.data.get("entities", []), indent=2)

            elif name == "queueStatus":
                return json.dumps(self.queue.status(), indent=2)

            elif name == "queueClear":
                count = await self.queue.clear()
                return f"Cleared {count} pending actions"

            elif name == "queueRemove":
                action_id = input_data.get("id", "")
                found = await self.queue.cancel(action_id)
                return f"Cancelled action {action_id}" if found else f"Action {action_id} not found"

            elif name == "stop":
                await self.queue.interrupt()
                await self.bridge.stop()
                return "Stopped all actions and movement"

            else:
                return f"Unknown tool: {name}"

        except Exception as e:
            logger.error(f"Tool error ({name}): {e}")
            return f"Error: {e}"

    async def _on_subaction(
        self, sub_id: str, name: str, args: dict | None, status: str, **kwargs: Any
    ) -> None:
        """Callback from instrumented primitives — forwards to queue."""
        await self.queue.record_subaction(sub_id, name, args, status, **kwargs)

    async def _on_action_started(self, event: str, action, *_extra) -> None:
        logger.info(f"Action {action.id} STARTED: {action.code[:200]}")

    async def _on_action_completed(self, event: str, action, *_extra) -> None:
        elapsed = (action.finished_at - action.started_at) if action.started_at and action.finished_at else 0
        if action.status.value == "completed":
            logger.info(f"Action {action.id} COMPLETED ({elapsed:.1f}s): {action.result or 'no output'}")
        else:
            logger.warning(f"Action {action.id} {action.status.value.upper()} ({elapsed:.1f}s): {action.error or 'no error'}")

    async def _execute_action(self, code: str) -> str:
        """Execute action code in the sandbox. Injected as queue executor."""
        try:
            return await execute(code, self.primitives)
        except SandboxError as e:
            return f"Sandbox error: {e}"

    async def _send_chat(self, text: str) -> None:
        """Send a message in-game, splitting if needed."""
        text = text.strip()
        if not text:
            return

        # Split at sentence boundaries or hard limit
        while text:
            if len(text) <= CHAT_MAX_LEN:
                await self.bridge.chat(text)
                break
            # Try to split at last space before limit
            split_at = text.rfind(" ", 0, CHAT_MAX_LEN)
            if split_at == -1:
                split_at = CHAT_MAX_LEN
            await self.bridge.chat(text[:split_at])
            text = text[split_at:].lstrip()

    def _trim_history(self, max_messages: int = 50) -> None:
        """Keep conversation history bounded."""
        if len(self.messages) > max_messages:
            # Keep the most recent messages
            self.messages = self.messages[-max_messages:]
