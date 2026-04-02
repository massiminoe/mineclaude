# Mineclaude

Minecraft bot — Python agent that uses Claude to control a headless MC client.

## Commands

- `pytest` — run tests
- `docker compose up --build` — run full stack (MC server + headless client w/ bridge)
- `docker compose down -v` — full clean restart (clears volumes, regenerates ops)
- `mineclaude` — run the agent process (requires `.env` with `ANTHROPIC_API_KEY`)
- `MOCK_BRIDGE=1 mineclaude` — test agent loop without MC server
- `cd frontend && npm run dev` — run frontend dev server (proxies to agent on port 3000)

## Project Structure

- `agent/` — Python package (bridge, sandbox, primitives, claude, agent, prompt, main, monitor)
- `bridge/` — HTTP+WS bridge server (runs inside MC client container, NOT installed locally)
  - `player_control.py` — shared helpers (look_at, find_slot, navigate, etc.)
  - `recipes.py` — crafting recipe table (~30 essential survival recipes)
  - `screenshot.py` — screenshot capture via Minescript + Pillow
- `frontend/` — React + TypeScript + Vite monitor UI
- `tests/` — pytest-asyncio tests (asyncio_mode = "auto")
- `mc-client/` — Dockerfile, entrypoint, mods, Minescript scripts
- `docker-compose.yml` — `itzg/minecraft-server` + custom `mc-client/Dockerfile`

## Key Patterns

- Protocol-based bridge (mock/real share interface)
- Executor injection on ActionQueue (decoupled from sandbox)
- exec() sandbox with AST validation (no imports, no dunders)
- Static system prompt enables Anthropic prompt caching
- gameState injected as synthetic tool_use/tool_result pair per turn
- `.env` file loaded at startup (not committed, see `.env.example`)

## Tech

- Python 3.13, deps: aiohttp, anthropic, httpx, websockets
    - use the virtual environment at .venv/
- Entry point: `mineclaude = "agent.main:main"`
- Monitor: aiohttp server on port 5555 (MONITOR_PORT) inside agent process
- Frontend: React + Vite dev server on port 5173, proxies `/api` to monitor
  - `cd frontend && npm run dev` — dev server
  - `cd frontend && npx vite build` — production build (served by monitor)
- Bridge: aiohttp server on port 8080 inside mc-client container
- Bridge logs to `/tmp/bridge.log` inside container (NOT to MC chat, to avoid feedback loops)

## Bridge API

- `GET /status` — player position, health, hunger, inventory, time
- `GET /nearby/blocks?r=8` — blocks within radius
- `GET /nearby/entities?r=32` — entities within radius
- `POST /goto` `{x, y, z}` — Baritone pathfinding
- `POST /mine` `{block}` — Baritone mining
- `POST /follow` `{player}` — Baritone follow (`#follow player <name>`)
- `POST /stop` — Baritone stop
- `POST /chat` `{message}` — send chat via `/tellraw` (avoids signed chat issues)
- `POST /place`, `/break`, `/craft`, `/equip`, `/discard` — MVP via server commands (`/give`, `/setblock`, etc.)
- `GET /screenshot` — capture game view (returns base64 JPEG, or raw with `?raw=true`)
- `GET /video/stream` — MJPEG video stream of game view
- `WS /events` — chat event stream

## Infrastructure Gotchas

- MC **1.21.5** (NOT 1.21.6), Fabric Loader 0.18.4, Fabric API 0.128.2
- HMC config: `/headlessmc/HeadlessMC/config.properties` (NOT `/root/HeadlessMC/`)
- Do NOT pass `-D` flags to `hmc` CLI — crashes silently. Use config.properties.
- Launch: `hmc launch fabric:1.21.5 -offline -inmemory` (no -lwjgl, renders to Xvfb)
- Game dir: `/headlessmc/HeadlessMC/run`
- Scripts: `/headlessmc/minescript/` (NOT game dir)
- Python 3 must be installed in Docker image for .py Minescript scripts
- Baritone commands: `#goto X Y Z`, `#mine <block>`, `#follow player <name>`, `#stop`
- Baritone v1.14.0, Minescript 5.0b11, hmc-specifics 2.3.0
- HeadlessMC 2.8.0 (`3arthqu4ke/headlessmc:latest`)
- Rendering via Xvfb virtual framebuffer + Mesa llvmpipe (software OpenGL 4.5)
- `hmc.check.xvfb=true` in config.properties, `LIBGL_ALWAYS_SOFTWARE=1` env var
- `minescript.screenshot(filename)` — native MC screenshot API (saves PNG to screenshots/ dir)
- Vision: Claude `screenshot` tool sends game view as base64 JPEG in tool_result image block

## Minescript v5.0 API Notes

- `minescript.player()` returns `EntityData` dataclass, NOT a tuple — use `player_position()` for `[x, y, z]`
- `minescript.entities()` returns `List[EntityData]` — access `.position`, `.name`, `.type`, `.health`
- `minescript.player_inventory()` returns `List[ItemStack]` — access `.item`, `.count`, `.slot`
- `minescript.world_info()` returns `WorldInfo` dataclass — use `.day_ticks`, `.raining`, etc.
- `minescript.player_biome()` does NOT exist in v5.0
- Chat events: use `EventQueue` + `register_chat_listener()`, NOT `chat_events()`
- `ChatEvent` may arrive as dict — check `isinstance(ev, dict)` before `hasattr(ev, "message")`
- Chat messages include `[Not Secure]` prefix with `ONLINE_MODE=false` — use regex to find `<Player>` pattern
- Player-control APIs take `pressed: bool` arg: `player_press_attack(True/False)`, `player_press_use(True/False)`, etc.
- `player_look_at(x, y, z)` exists — use it instead of manual yaw/pitch math
- `player_inventory_select_slot(slot)` — selects hotbar slot (NOT `player_select_slot`)
- `player_inventory_slot_to_hotbar(slot)` — exists but BROKEN on MC 1.21.5 (ServerboundPickItemPacket removed in 1.21.4)
- `container_click`, `close_screen`, `player_press_inventory`, `open_inventory` — do NOT exist in v5.0b11
- `GET /probe` endpoint — returns JSON of all available Minescript APIs and capabilities

## Known Workarounds

- **Signed chat crash**: `ONLINE_MODE=false` breaks MC signed chat on 2nd+ message. Bot sends via `/tellraw @a` instead of `minescript.chat()`
- **Emojis**: MC can't render them — stripped to ASCII before sending, prompt tells Claude not to use them
- **Bot opping**: `OPS` env var unreliable with offline-mode. RCON ops both bot and player in entrypoint after connection
- **Bridge logging**: Must NOT use Python `logging` to stdout (Minescript routes it to MC chat → feedback loop). Logs to `/tmp/bridge.log`
- **break/place/attack**: Real player actions (look_at + press_attack/press_use). Verified working in-game
- **discard**: Real (select slot + press_drop). Works if item already in hotbar
- **equip hand/offhand**: Real (inventory_select_slot / swap_hands). Works if item in hotbar
- **equip armor**: Fallback only (/item replace) — no container_click API
- **craft**: Simulated (/clear ingredients + /give output) — validates recipe and ingredients, no container_click/close_screen APIs
- Items not in hotbar can't be moved there (player_inventory_slot_to_hotbar broken on MC 1.21.5)
- The `method` field in response dicts indicates "real" or "fallback" path
