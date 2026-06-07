"""Entry point for the Mineclaude agent."""

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


def _run_mcp_mode(
    logger: logging.Logger,
    *,
    mock_bridge: bool,
    bridge_url: str,
    bridge_ws_url: str,
    bot_name: str,
) -> None:
    """Co-hosted MCP launcher: one process, one shared Runtime, three faces —
    the MCP server (streamable-HTTP, for Claude Code), the aiohttp monitor, and
    the bridge event subscription. No brain, no LLM provider. This is the path
    the project converges on; the Claude-loop branch below is deleted in P5.

    The Runtime owns the WS subscription here (runtime.run_events) — the P3
    "subscription flip" the brain path still defers."""
    from agent import mcp_server
    from agent.bridge import create_bridge
    from agent.monitor import MonitorServer
    from agent.runtime import Runtime

    monitor_port = int(os.environ.get("MONITOR_PORT", "5555"))
    mcp_host = os.environ.get("MCP_HOST", "127.0.0.1")
    mcp_port = int(os.environ.get("MCP_PORT", "5556"))

    bridge = create_bridge(mock=mock_bridge, base_url=bridge_url, ws_url=bridge_ws_url)
    runtime = Runtime(bridge)
    monitor = MonitorServer(runtime, port=monitor_port)

    logger.info(
        f"Mineclaude MCP mode (mock={mock_bridge}, bot={bot_name}) — "
        f"monitor http://0.0.0.0:{monitor_port}, MCP http://{mcp_host}:{mcp_port}/mcp"
    )

    async def run() -> None:
        runtime.start()
        await monitor.start()
        # Cut a fresh recording for this run (best-effort; no-op on mock /
        # when RECORD_VIDEO=0). Mirrors the brain launcher.
        if not mock_bridge:
            try:
                await bridge.record_roll()
                logger.info("rolled gameplay recording for this run")
            except Exception as e:
                logger.warning(f"recording roll skipped: {e}")
        # Both run forever: the MCP HTTP server and the bridge event loop
        # (reconnecting WS). gather lets either's failure surface + exit.
        await asyncio.gather(
            mcp_server.serve(runtime, host=mcp_host, port=mcp_port),
            runtime.run_events(),
        )

    asyncio.run(run())


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
    # MCP mode: drive the bot from an external agent (Claude Code) over MCP
    # instead of the built-in Claude loop. The end-state path — the brain
    # branch below is deleted in P5.
    mcp_mode = os.environ.get("MCP", "").lower() in ("1", "true", "yes")
    # Native Fabric mod owns every endpoint after Phase 8. HTTP on 8081
    # (JDK HttpServer), events WS on 8082 (Java-WebSocket — separate
    # listener because JDK HttpServer doesn't speak WS upgrades).
    bridge_url = os.environ.get("BRIDGE_URL", "http://localhost:8081")
    bridge_ws_url = os.environ.get("BRIDGE_WS_URL", "ws://localhost:8082/events")
    bot_name = os.environ.get("BOT_NAME", "Claude")

    if mcp_mode:
        _run_mcp_mode(
            logger,
            mock_bridge=mock_bridge,
            bridge_url=bridge_url,
            bridge_ws_url=bridge_ws_url,
            bot_name=bot_name,
        )
        return
    # LLM_PROVIDER selects the model + endpoint (see agent/providers.py).
    # CLAUDE_MODEL / FIREWORKS_MODEL still override the model within a provider.
    llm_provider_name = os.environ.get("LLM_PROVIDER", "anthropic")
    # Compaction defaults to the main model when unset. Set COMPACTION_MODEL
    # to e.g. claude-haiku-4-5-20251001 to run compaction on a cheaper model.
    compaction_model = os.environ.get("COMPACTION_MODEL") or None

    # Resolve the provider early to validate its API key. resolve_provider only
    # reads env + a static table (no AsyncAnthropic), so importing it before the
    # heavy agent modules + Langfuse init is fine.
    from agent.providers import resolve_provider

    try:
        provider = resolve_provider(llm_provider_name)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)
    api_key = provider.api_key()

    if not mock_bridge and not no_claude and not api_key:
        logger.error(
            f"{provider.api_key_env} is required for LLM_PROVIDER={provider.name} "
            f"(or set MOCK_BRIDGE=1 or NO_CLAUDE=1)"
        )
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

    bridge = create_bridge(
        mock=mock_bridge,
        base_url=bridge_url,
        ws_url=bridge_ws_url,
    )
    claude = None if no_claude else ClaudeClient(provider, api_key=api_key)
    agent = Agent(
        bridge=bridge,
        claude=claude,
        bot_name=bot_name,
        compaction_model=compaction_model,
    )
    monitor = MonitorServer(agent, port=monitor_port)

    logger.info(
        f"Mineclaude agent starting (mock={mock_bridge}, no_claude={no_claude}, "
        f"bot={bot_name}, provider={provider.name}, "
        f"model={'<disabled>' if no_claude else provider.model}, "
        f"compaction_model={compaction_model or provider.model})"
    )

    try:
        # In mock mode, inject a test chat event after a short delay
        async def run():
            await monitor.start()
            # Cut a fresh gameplay recording for this agent run. The bridge mod
            # auto-starts one .mp4 when the container comes up; rolling here
            # gives THIS mineclaude process its own file without a container
            # restart (see RecordRoute). Best-effort + self-gating: it's a no-op
            # on the mod side when nothing's recording (RECORD_VIDEO=0), and a
            # mock bridge / unreachable container just logs and moves on.
            if not mock_bridge:
                try:
                    await bridge.record_roll()
                    logger.info("rolled gameplay recording for this run")
                except Exception as e:
                    logger.warning(f"recording roll skipped: {e}")
            if not no_claude and mock_bridge and isinstance(bridge, MockBridgeClient):
                async def inject_after_delay():
                    await asyncio.sleep(1.0)
                    logger.info("Injecting test chat event")
                    bridge.inject_chat("Steve", "Hey Claude, can you get me some oak logs?")
                asyncio.create_task(inject_after_delay())
            await agent.start(handle_chat=not no_claude)

        asyncio.run(run())
    finally:
        if not no_claude:
            _shutdown_langfuse(logger)


if __name__ == "__main__":
    main()
