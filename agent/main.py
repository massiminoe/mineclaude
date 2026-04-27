"""Entry point for the Mineclaw agent."""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys


def _load_dotenv() -> None:
    """Load .env file from project root if it exists."""
    env_file = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def _init_langfuse(logger: logging.Logger) -> None:
    """Initialize Langfuse tracing if configured. Must be called BEFORE creating AsyncAnthropic."""
    if not os.environ.get("LANGFUSE_SECRET_KEY"):
        logger.debug("Langfuse not configured (LANGFUSE_SECRET_KEY not set)")
        return

    try:
        from langfuse import Langfuse
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

        Langfuse()
        AnthropicInstrumentor().instrument()
        logger.info("Langfuse tracing enabled")
    except Exception as e:
        logger.warning(f"Failed to initialize Langfuse: {e}")


def _shutdown_langfuse(logger: logging.Logger) -> None:
    """Flush Langfuse on shutdown to avoid losing traces."""
    try:
        from langfuse import get_client

        client = get_client()
        if client:
            client.flush()
            logger.debug("Langfuse flushed")
    except Exception:
        pass


def main() -> None:
    _load_dotenv()

    # Configure logging
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("agent")

    # Read config from environment
    mock_bridge = os.environ.get("MOCK_BRIDGE", "").lower() in ("1", "true", "yes")
    no_claude = os.environ.get("NO_CLAUDE", "").lower() in ("1", "true", "yes")
    bridge_url = os.environ.get("BRIDGE_URL", "http://localhost:8080")
    bot_name = os.environ.get("BOT_NAME", "Mineclaw")
    claude_model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not mock_bridge and not no_claude and not api_key:
        logger.error("ANTHROPIC_API_KEY is required (or set MOCK_BRIDGE=1 or NO_CLAUDE=1)")
        sys.exit(1)

    # Initialize Langfuse BEFORE importing modules that create AsyncAnthropic
    if not no_claude:
        _init_langfuse(logger)

    # Import here to avoid import-time side effects
    from agent.agent import Agent
    from agent.bridge import MockBridgeClient, create_bridge
    from agent.claude import ClaudeClient
    from agent.monitor import MonitorServer

    monitor_port = int(os.environ.get("MONITOR_PORT", "5555"))

    bridge = create_bridge(mock=mock_bridge, base_url=bridge_url)
    claude = None if no_claude else ClaudeClient(model=claude_model, api_key=api_key)
    agent = Agent(bridge=bridge, claude=claude, bot_name=bot_name)
    monitor = MonitorServer(agent, port=monitor_port)

    logger.info(
        f"Mineclaw agent starting (mock={mock_bridge}, no_claude={no_claude}, "
        f"bot={bot_name}, model={'<disabled>' if no_claude else claude_model})"
    )

    try:
        # In mock mode, inject a test chat event after a short delay
        async def run():
            await monitor.start()
            if not no_claude and mock_bridge and isinstance(bridge, MockBridgeClient):
                async def inject_after_delay():
                    await asyncio.sleep(1.0)
                    logger.info("Injecting test chat event")
                    bridge.inject_chat("Steve", "Hey Mineclaw, can you get me some oak logs?")
                asyncio.create_task(inject_after_delay())
            await agent.start(handle_chat=not no_claude)

        asyncio.run(run())
    finally:
        if not no_claude:
            _shutdown_langfuse(logger)


if __name__ == "__main__":
    main()
