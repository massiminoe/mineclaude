"""Core agent loop: chat → Claude → tool_use → execute → respond."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable, Coroutine

from agent.action_queue import ActionQueue
from agent.bridge import BridgeClient
from agent.claude import ClaudeClient
from agent.primitives import make_primitives
from agent.plan import read_plan, write_plan
from agent.prompt import build_system_prompt, format_game_state
from agent.sandbox import SandboxError, execute
from agent.session_log import SessionLogger

try:
    from langfuse import observe, propagate_attributes, get_client
    _langfuse_available = True
except ImportError:
    _langfuse_available = False
    propagate_attributes = None
    get_client = None
    def observe(*args, **kwargs):
        """No-op decorator when langfuse is not installed."""
        if args and callable(args[0]):
            return args[0]
        def decorator(fn):
            return fn
        return decorator

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 20
CHAT_MAX_LEN = 240
LOG_TRIM = 4096
# Settle delay after a newAction completes, before the next Claude iteration
# reads gameState. MC has a few-tick lag between drop auto-pickup and
# player_inventory() reflecting it — this gives the cache a chance to catch up.
ACTION_SETTLE_S = 0.3


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
        self._session_loggers: dict[str, SessionLogger] = {}
        self._current_session_logger: SessionLogger | None = None
        # Most recent status dict that Claude was shown via gameState injection.
        # Read by the monitor's belief checker to diff against live bridge state.
        self.last_injected_status: dict[str, Any] | None = None
        # Monotonic timestamp of the agent's last active moment (chat in or tool
        # return). Read by the monitor to suppress belief mismatches while the
        # agent is idle — state drifts naturally when no one is looking.
        self.last_activity_ts: float = time.monotonic()
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

        self.last_activity_ts = time.monotonic()

        session_id = self._session_ids.setdefault(username, str(uuid.uuid4()))
        if session_id not in self._session_loggers:
            self._session_loggers[session_id] = SessionLogger(session_id)
        self._current_session_logger = self._session_loggers[session_id]

        if _langfuse_available and propagate_attributes:
            with propagate_attributes(user_id=username, session_id=session_id):
                await self._handle_chat_traced(data)
        else:
            await self._handle_chat_traced(data)

    def _slog(self, event: str, **data: Any) -> None:
        """Emit to the current session logger (no-op if none is set)."""
        if self._current_session_logger is not None:
            self._current_session_logger.emit(event, **data)

    @observe()
    async def _handle_chat_traced(self, data: dict) -> None:
        username = data.get("username", "")
        message = data.get("message", "")

        logger.info(f"Chat from {username}: {message}")

        self._slog("chat_in", username=username, message=message)

        # Append user message
        self.messages.append({
            "role": "user",
            "content": f"{username}: {message}",
        })

        # Inject synthetic plan tool_use/tool_result pair (fresh from disk each turn).
        # Plan is chat-level; gameState refreshes per Claude iteration below.
        self._inject_plan()

        await self._emit("conversation:update", self.messages)

        # Claude loop
        for iteration in range(MAX_ITERATIONS):
            logger.info(f"Claude iteration {iteration + 1}/{MAX_ITERATIONS}")
            self.last_activity_ts = time.monotonic()

            # Refresh gameState on every iteration so Claude sees the current
            # world, not a snapshot from 10 tool calls ago. Each iteration gets
            # a unique tool_use_id so the prompt cache prefix only diverges at
            # the latest injection.
            status_resp = await self.bridge.get_status()
            queue_status = self.queue.status()
            game_state = format_game_state(status_resp.data, queue_status)
            self.last_injected_status = status_resp.data
            self._inject_gamestate(game_state, iteration)
            await self._emit("conversation:update", self.messages)

            self._slog(
                "claude_request",
                iteration=iteration + 1,
                max_iterations=MAX_ITERATIONS,
                message_count=len(self.messages),
                game_state=game_state,
                status=status_resp.data,
                queue=queue_status,
            )

            try:
                response = await self.claude.send(self.system_prompt, self.messages)
            except Exception as e:
                logger.error(f"Claude API error: {e}")
                self._slog("exception", stage="claude_send", exc=type(e).__name__, message=str(e))
                await self._send_chat("Sorry, I had a brain glitch. Try again?")
                break

            logger.info(f"Claude response: stop_reason={response.stop_reason}, blocks={len(response.content)}")
            self._slog(
                "claude_response",
                iteration=iteration + 1,
                stop_reason=response.stop_reason,
                blocks=[
                    {
                        "type": b.type,
                        "text": getattr(b, "text", None) if b.type == "text" else None,
                        "tool_name": getattr(b, "name", None) if b.type == "tool_use" else None,
                        "tool_input": getattr(b, "input", None) if b.type == "tool_use" else None,
                    }
                    for b in response.content
                ],
            )

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
                    _t0 = time.monotonic()
                    result = await self._dispatch_tool(tool_use.name, tool_use.input)
                    _elapsed_ms = int((time.monotonic() - _t0) * 1000)
                    self._slog(
                        "tool_dispatch",
                        iteration=iteration + 1,
                        name=tool_use.name,
                        tool_use_id=tool_use.id,
                        input=tool_use.input,
                        result=(
                            {"type": "image", "text": result.get("text", "")}
                            if isinstance(result, dict) and result.get("_type") == "image_tool_result"
                            else result
                        ),
                        elapsed_ms=_elapsed_ms,
                    )
                    if isinstance(result, dict) and result.get("_type") == "image_tool_result":
                        # Image tool result — send as multi-part content with image block
                        media_type = f"image/{result.get('format', 'jpeg')}"
                        logger.info(f"Tool result ({tool_use.name}): [image {result.get('text', '')}]")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": result["image"],
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": result.get("text", "Screenshot captured."),
                                },
                            ],
                        })
                    else:
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
                self._slog("chat_out", text=full_text, iteration=iteration + 1)
                await self._send_chat(full_text)
            break

        # Trim conversation history to avoid unbounded growth
        self._trim_history()

    @observe(as_type="tool")
    async def _dispatch_tool(self, name: str, input_data: dict) -> str | dict:
        """Route tool calls to appropriate handlers.

        Returns str for text results, or dict with _type='image_tool_result'
        for image results (screenshot).
        """
        logger.debug(f"Tool call: {name}({json.dumps(input_data)[:LOG_TRIM]})")

        if _langfuse_available and get_client is not None:
            try:
                get_client().update_current_span(name=f"tool.{name}")
            except Exception:
                pass

        try:
            if name == "newAction":
                code = input_data.get("code", "")
                action = await self.queue.enqueue(code)
                await self.queue.drain()
                # Settle: MC has a tick-level lag between "drop auto-pickup
                # consumed" and "player_inventory() reflects it". Waiting 300ms
                # here gives delayed pickups time to register before the next
                # iteration fetches gameState — otherwise Claude sees stale
                # inventory counts and may re-do work unnecessarily.
                await asyncio.sleep(ACTION_SETTLE_S)
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

            elif name == "writePlan":
                content = input_data.get("content", "")
                lines = write_plan(content)
                await self._emit("plan:update", content)
                if lines == 0:
                    return "plan cleared"
                return f"plan saved ({lines} lines)"

            elif name == "screenshot":
                resp = await self.bridge.screenshot()
                if resp.status != "success":
                    return f"Screenshot failed: {resp.message}"
                return {
                    "_type": "image_tool_result",
                    "image": resp.data["image"],
                    "format": resp.data.get("format", "jpeg"),
                    "text": f"Screenshot captured ({resp.data.get('width', '?')}x{resp.data.get('height', '?')})",
                }

            else:
                return f"Unknown tool: {name}"

        except Exception as e:
            logger.error(f"Tool error ({name}): {e}")
            self._slog(
                "exception",
                stage="dispatch_tool",
                tool=name,
                exc=type(e).__name__,
                message=str(e),
            )
            return f"Error: {e}"

    async def _on_subaction(
        self, sub_id: str, name: str, args: dict | None, status: str, **kwargs: Any
    ) -> None:
        """Callback from instrumented primitives — forwards to queue.

        Also emits a `subaction` session-log entry so post-hoc analysis can
        see which specific primitive (goToPosition, breakBlockAt, ...) in a
        multi-step newAction chain failed, without having to parse the
        error string.
        """
        self._slog(
            "subaction",
            id=sub_id,
            name=name,
            args=args,
            status=status,
            result=kwargs.get("result"),
            error=kwargs.get("error"),
        )
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

    def _inject_gamestate(self, game_state: str, iteration: int) -> None:
        """Append a synthetic gameState tool_use/tool_result pair to messages.

        Called once per Claude iteration so the agent always sees current
        world state. Each call uses a unique tool_use_id (`gamestate_auto_<n>`)
        — by only ever appending (never rewriting history), we preserve the
        prompt cache prefix for every message before this one.
        """
        tool_id = f"gamestate_auto_{iteration}"
        self.messages.append({
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tool_id, "name": "gameState", "input": {}}
            ],
        })
        self.messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": game_state}
            ],
        })

    def _inject_plan(self) -> None:
        """Inject the current plan document as a synthetic tool_use/tool_result pair.

        Re-reads ./state/plan.md fresh from disk each call so the agent always
        sees the latest plan (including any edits made by a previous writePlan
        call, or by the user directly on disk).
        """
        plan_text = read_plan()
        body = plan_text if plan_text else "(no active plan)"
        wrapped = f"<plan_document>\n{body}\n</plan_document>"

        self.messages.append({
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "plan_auto",
                    "name": "plan",
                    "input": {},
                }
            ],
        })
        self.messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "plan_auto",
                    "content": wrapped,
                }
            ],
        })

    def _trim_history(self, max_messages: int = 50) -> None:
        """Keep conversation history bounded."""
        if len(self.messages) > max_messages:
            # Keep the most recent messages
            self.messages = self.messages[-max_messages:]
