"""Entry point for the Mineclaude MCP launcher.

One process, one shared Runtime, three faces:
  * the MCP server (streamable-HTTP) an external agent — Claude Code — drives,
  * the aiohttp monitor (observability + Console), and
  * the bridge event subscription (Runtime owns it via run_events).

MCP is the only way to drive the bot. The built-in Claude loop and every LLM
provider integration were removed in the brain teardown — `mineclaude/runtime.py`
is the body, `mineclaude/mcp_server.py` the interface.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys
import uuid


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


# The bot's custom skin is broadcast server-side by Fabric Tailor (mc-server runs
# TYPE=FABRIC). Fabric Tailor persists it in the world's playerdata, so a normal
# relaunch keeps it — but `docker compose down -v` wipes the world and the bot
# reverts to default Steve. Re-assert the skin every launch so a clean restart
# self-heals; it's idempotent (Fabric Tailor just re-sets the same texture) and a
# no-op when already set. The URL is a Mojang-hosted upload of skins/claude_crab.png
# (via MineSkin). Override with SKIN_TEXTURE_URL / SKIN_MODEL; set the URL to "" to
# disable. To change the skin: replace skins/claude_crab.png, re-upload it at
# https://mineskin.org, and paste the new textures.minecraft.net URL below.
DEFAULT_SKIN_URL = (
    "https://textures.minecraft.net/texture/"
    "6c75af04ca959367bb55dec6beb0f4b57dec5451481cc399a6443ef510d84258"
)


async def _reapply_skin(bridge, url: str, model: str, logger) -> None:
    """Re-assert the bot's broadcast skin at launch. Waits until the bot is
    in-world (the bridge HTTP is up before the server join finishes), then runs
    Fabric Tailor's /skin command as the bot (it's opped)."""
    for _ in range(20):
        try:
            st = await bridge.get_status()
            if st.status == "success" and st.data.get("health") is not None:
                break
        except Exception:
            pass
        await asyncio.sleep(1.0)
    await bridge.chat(f"/skin set URL {model} {url}")
    logger.info("re-applied bot skin (broadcast via Fabric Tailor)")


def main() -> None:
    _load_dotenv()

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger = logging.getLogger("mineclaude")

    # Config from environment.
    mock_bridge = os.environ.get("MOCK_BRIDGE", "").lower() in ("1", "true", "yes")
    # Native Fabric mod owns every endpoint after Phase 8. HTTP on 8081
    # (JDK HttpServer), events WS on 8082 (Java-WebSocket — separate listener
    # because JDK HttpServer doesn't speak WS upgrades).
    bridge_url = os.environ.get("BRIDGE_URL", "http://localhost:8081")
    bridge_ws_url = os.environ.get("BRIDGE_WS_URL", "ws://localhost:8082/events")
    bot_name = os.environ.get("BOT_NAME", "Claude")
    monitor_port = int(os.environ.get("MONITOR_PORT", "5555"))
    mcp_host = os.environ.get("MCP_HOST", "127.0.0.1")
    mcp_port = int(os.environ.get("MCP_PORT", "5556"))
    skin_url = os.environ.get("SKIN_TEXTURE_URL", DEFAULT_SKIN_URL)
    skin_model = os.environ.get("SKIN_MODEL", "classic")

    from mineclaude import mcp_server
    from mineclaude.bridge import create_bridge
    from mineclaude.monitor import MonitorServer
    from mineclaude.runtime import Runtime
    from mineclaude.session_log import SessionLogger

    # Session log: one JSONL per launch under state/sessions/ (replayable via
    # `python scripts/session_report.py --latest`). The Runtime drops traces when
    # slog is None, so SESSION_LOG=0 opts out cleanly.
    slog = None
    session_log = None
    if os.environ.get("SESSION_LOG", "1").lower() not in ("0", "false", "no"):
        session_log = SessionLogger(str(uuid.uuid4()))
        slog = session_log.emit

    bridge = create_bridge(mock=mock_bridge, base_url=bridge_url, ws_url=bridge_ws_url)
    runtime = Runtime(bridge, slog=slog)
    monitor = MonitorServer(runtime, port=monitor_port)

    logger.info(
        f"Mineclaude MCP launcher (mock={mock_bridge}, bot={bot_name}) — "
        f"monitor http://0.0.0.0:{monitor_port}, MCP http://{mcp_host}:{mcp_port}/mcp"
    )
    if session_log is not None:
        logger.info(f"session log: {session_log.path}")

    async def run() -> None:
        runtime.start()
        await monitor.start()
        # Cut a fresh recording for this run (best-effort; no-op on mock /
        # when RECORD_VIDEO=0).
        if not mock_bridge:
            try:
                await bridge.record_roll()
                logger.info("rolled gameplay recording for this run")
            except Exception as e:
                logger.warning(f"recording roll skipped: {e}")
            # Re-assert the bot's broadcast skin (self-heals after `down -v`).
            if skin_url:
                try:
                    await _reapply_skin(bridge, skin_url, skin_model, logger)
                except Exception as e:
                    logger.warning(f"skin re-apply skipped: {e}")
        # Both run forever: the MCP HTTP server and the bridge event loop
        # (reconnecting WS). gather lets either's failure surface and exit.
        await asyncio.gather(
            mcp_server.serve(runtime, host=mcp_host, port=mcp_port),
            runtime.run_events(),
        )

    asyncio.run(run())


if __name__ == "__main__":
    main()
