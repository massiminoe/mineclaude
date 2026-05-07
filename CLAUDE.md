# Mineclaude

Minecraft bot — Python agent that uses Claude to control a headless MC client.

## Commands

- `pytest` — run tests
- `docker compose up --build` — run full stack (MC server + headless client w/ native bridge mod)
- `docker compose down -v` — full clean restart (clears volumes, regenerates ops)
- `mineclaude` — run the agent process (requires `.env` with `ANTHROPIC_API_KEY`)
- `MOCK_BRIDGE=1 mineclaude` — test agent loop without MC server
- `BRIDGE_URL` (env, default `http://localhost:8081`) — native bridge HTTP
- `BRIDGE_WS_URL` (env, default `ws://localhost:8082/events`) — native bridge events WS
- `NO_CLAUDE=1 mineclaude` — headless mode (no Claude); queue + bridge + monitor stay up so you can drive primitives manually from the frontend Console panel
- `cd frontend && npm run dev` — run frontend dev server (proxies to agent on port 3000)

## Project Structure

- `agent/` — Python package (bridge, sandbox, primitives, claude, agent, prompt, main, monitor)
- `frontend/` — React + TypeScript + Vite monitor UI
- `tests/` — pytest-asyncio tests (asyncio_mode = "auto")
- `mc-client/` — Dockerfile + entrypoint for the headless MC client container that runs the bridge mod
- `mc-mod/` — Kotlin Fabric mod (`mineclaude-bridge`). Owns every bridge endpoint the agent + frontend hit. Built in stage 1 of `mc-client/Dockerfile`. HTTP on 8081 (JDK HttpServer), events WS on 8082 (Java-WebSocket). The Phase 0–8 migration history lives in `docs/superpowers/specs/2026-04-*-native-mod-*`
- `docker-compose.yml` — `itzg/minecraft-server` + custom `mc-client/Dockerfile`

## Key Patterns

- Protocol-based bridge (mock/real share interface)
- Executor injection on ActionQueue (decoupled from sandbox)
- exec() sandbox with AST validation (no imports, no dunders)
- Static system prompt enables Anthropic prompt caching
- gameState injected as synthetic tool_use/tool_result pair on EVERY Claude iteration (not just once per chat turn) — unique `gamestate_auto_<iter>` tool_use_id keeps the cache prefix stable through prior messages and only diverges at the latest injection. Prevents Claude from deciding on a 10-iteration-old snapshot
- Plan document (`state/plan.md`) injected chat-level via the same synthetic pair mechanism
- `.env` file loaded at startup (not committed, see `.env.example`)

## Tech

- Python 3.13, deps: aiohttp, anthropic, httpx, websockets
    - use the virtual environment at .venv/
- Entry point: `mineclaude = "agent.main:main"`
- Monitor: aiohttp server on port 5555 (MONITOR_PORT) inside agent process
- Frontend: React + Vite dev server on port 5173, proxies `/api` to monitor
  - `cd frontend && npm run dev` — dev server
  - `cd frontend && npx vite build` — production build (served by monitor)
- Bridge: Kotlin Fabric mod (`mineclaude-bridge`) running in-process to MC. JDK HttpServer on port 8081 for HTTP, separate Java-WebSocket listener on 8082 for `/events` (JDK HttpServer doesn't speak WS upgrades). All routes live in `mc-mod/src/main/kotlin/com/mineclaude/bridge/*Route.kt`; `HttpBridge.kt` dispatches them. Handlers needing MC state submit to the client tick thread via `TickThread.submitAndWait(timeoutMs) { … }` so the HttpServer worker pool stays free for incoming requests
- **How endpoints are implemented:** Inventory writes (`/equip`, `/discard`) use `interactionManager.clickSlot` PICKUP/SWAP so they cover non-hotbar items + armor in one path. World mutations drive `interactionManager.attackBlock` + `updateBlockBreakingProgress` (`/break`, with denylist-gated recursive auto-clear of occluders to depth=2), `interactionManager.interactBlock` (`/place`, with synthetic BlockHitResult on a found adjacent solid), and `attackEntity` (`/attack`). Container ops (`/craft`, `/furnace/*`) open the screen handler via `interactBlock` and drive placement/extraction through a `MenuClicker.withOpenedBlock { … }` helper that always `closeHandledScreen`s in a `finally`; the recipe + smelting tables live in `Recipes.kt`. The 2×2 inventory crafter clicks PSH 1..4 directly without opening any UI. Movement endpoints (`/goto`, `/mine`, `/follow`, `/stop`, `/explore`) send `#…` chat strings via `player.networkHandler.sendChatMessage` so Baritone's client-side chat hook intercepts them. `/goto` polls `player.pos` directly for arrival; `/collect` runs the walk-loop in Kotlin against `world.entities`. Events WS hooks `ClientReceiveMessageEvents.CHAT` (player chat — `sender.name` authoritative; `GAME` kept as fallback for `/say`/`/tellraw`-wrapped chat) + `ClientTickEvents.END_CLIENT_TICK` (alive↔dead transitions). Vision (`/screenshot`, `/video/stream`) shells out to `ffmpeg -f x11grab -i :99` from Kotlin — `NativeImage.writeTo()` produces 0-byte PNGs on ARM64 Mesa llvmpipe, so direct framebuffer capture is the only reliable path. `/screenshot` returns JSON-wrapped base64 (or raw bytes with `?raw=true`); `/video/stream` runs one persistent ffmpeg per client and writes `multipart/x-mixed-replace` MJPEG over JDK HttpServer's chunked output
- Bridge logs through SLF4J to MC's standard log (visible via `docker compose logs mc-client`). The legacy Python bridge's `/tmp/bridge.log` and `/tmp/bridge.log.mutations.jsonl` are gone with Phase 8

## Bridge API

- `GET /status` — player position, health, hunger, inventory, time
- `GET /nearby/blocks?r=8` — blocks within radius
- `GET /nearby/entities?r=32` — entities within radius
- `POST /goto` `{x, y, z}` — Baritone pathfinding (polls arrival)
- `POST /mine` `{block}` — Baritone mining (fire-and-forget)
- `POST /follow` `{player}` — Baritone follow
- `POST /stop` / `POST /explore` — Baritone control
- `POST /chat` `{message}` — send chat (`#`/`\` via `sendChatMessage`, `/cmd` via `sendChatCommand`, plain text wrapped in `/tellraw` to dodge signed-chat disconnect)
- `POST /place`, `/break`, `/equip`, `/discard` — world + inventory mutations via `interactionManager`
- `POST /attack` `{entity_id}` — loops swings until target dies, despawns, leaves reach, or 30s elapses (auto-paths into melee). `POST /attack/stop` cancels the in-flight loop (used by reflex preempt)
- `POST /surface` — hold forward+jump+sprint via vanilla input keys to surface from full submersion (drowning escape; Baritone can't path from a fully-submerged start)
- `POST /craft` `{item, count}` — opens crafting screen, places ingredients, extracts output
- `POST /furnace/load`, `GET /furnace/inspect`, `POST /furnace/extract` — furnace lifecycle
- `POST /chest/store`, `POST /chest/take` `{x, y, z, items: [{name, count|"all"}]}`, `GET /chest/inspect?x&y&z` — chest I/O. Coords required (no nearest fallback — chests cluster). `count` accepts an int or `"all"`. Partial success is the response shape (`{stored|taken, skipped}`), not an error: chest-full or item-missing returns 200 with the actual delta. Single + double chests handled uniformly via `handler.slots.size − 36` (trailing 36 slots = main inv + hotbar)
- `POST /collect` `{radius}` — walk to and pick up dropped item entities within radius
- `GET /screenshot` — capture game view (returns base64 JPEG, or raw with `?raw=true`). Optional aim: `?yaw=&pitch=` (degrees) **or** `?look_at_x=&look_at_y=&look_at_z=` (point eye at a world coord). New rotation persists. Aimed shots add ~200ms (render-settle window) before the grab
- `GET /video/stream` — MJPEG video stream of game view
- `GET /health` — bridge liveness + ported endpoint list
- `WS /events` — chat / death / respawn event stream

## Infrastructure Gotchas

- MC **1.21.5** (NOT 1.21.6), Fabric Loader 0.18.4, Fabric API 0.128.2
- HMC config: `/headlessmc/HeadlessMC/config.properties` (NOT `/root/HeadlessMC/`)
- Do NOT pass `-D` flags to `hmc` CLI — crashes silently. Use config.properties.
- Launch: `hmc launch fabric:1.21.5 -offline -inmemory` (renders to Xvfb)
- Game dir: `/headlessmc/HeadlessMC/run`
- Baritone commands sent via chat: `#goto X Y Z`, `#mine <block>`, `#follow player <name>`, `#stop`, `#explore`
- Baritone v1.14.0, hmc-specifics 2.3.0, HeadlessMC 2.8.0 (`3arthqu4ke/headlessmc:latest`)
- Rendering via Xvfb virtual framebuffer + Mesa llvmpipe (software OpenGL 4.5)
- `hmc.check.xvfb=true` in config.properties, `LIBGL_ALWAYS_SOFTWARE=1` env var
- `NativeImage.writeTo()` produces 0-byte PNGs on the ARM64 Mesa stack — vision endpoints capture via ffmpeg x11grab from `:99` instead
- Vision: Claude `screenshot` tool sends game view as base64 JPEG in tool_result image block

## Bridge mod (mc-mod) gotchas

- **Tick-thread discipline:** any handler reading or mutating MC state must wrap its body in `TickThread.submitAndWait(timeoutMs) { … }`. Calling Minecraft APIs off the client tick thread mostly works, until it doesn't (concurrent ChunkManager access, partially-loaded entity views). The HttpServer worker pool is where handlers run; they submit a task and block until the next client tick services it
- **`interactionManager.clickSlot` for inventory writes:** PICKUP/SWAP covers non-hotbar items + armor in one path. Avoid the legacy `/item replace` shuffle — it's lossy on NBT
- **`break` occlusion handling:** `interactionManager.attackBlock` mines whatever the eye-ray hits, not the target coords. `BreakRoute` checks `crosshairTarget` after aiming and recursively auto-clears benign occluders (dirt above target stone) up to depth=2. `_OCCLUDER_DENYLIST` (containers, beds, doors, etc.) raises a prescriptive error so the agent decides what to do
- **Container ops never leak:** every screen-opening route uses `MenuClicker.withOpenedBlock { … }` which always `closeHandledScreen`s in a `finally`. Without this, a stale ScreenHandler silently no-ops subsequent world actions because input is captured by Screens
- **`/goto` held-slot leak:** Baritone auto-equips throwaway blocks while pathing. `GotoRoute` snapshots the held hotbar slot at entry and restores it on exit
- **Events WS:** `ClientReceiveMessageEvents.CHAT` (sender.name) is the authoritative player-chat hook. `GAME` is a fallback — only fires for server-origin messages (`/say`, `/tellraw`). The chat regex on GAME drops bot self-echoes because `/tellraw` text doesn't match `<Name> message`
- **Vision shell-out:** `ffmpeg -f x11grab -video_size 854x480 -i :99` runs on the HttpServer worker pool, not the tick thread (no MC state involved). Stderr drained concurrently to avoid pipe-fill deadlock; 5s timeout on `/screenshot`. `/video/stream` runs one persistent ffmpeg per client and SIGKILLs on disconnect (IOException on response write)

## Debugging

- `state/sessions/<ts>-<id>.jsonl` — agent-side replay log. Every turn, every Claude iteration, every tool call with timing, every belief mismatch. Emitted by `agent/session_log.py`. Render as a timeline: `python scripts/session_report.py --latest`
- Bridge-side logs go through SLF4J to MC's standard log — `docker compose logs mc-client` for the live stream, or filter for `mineclaude-bridge` loggers. The legacy mutation-log JSONL is gone with Phase 8; if you need before/after world snapshots, the agent's session log captures pre/post `/status` around each tool call

A **belief mismatch** (logged by `agent/belief_check.py`) means the agent's most recently injected gameState diverges from what the bridge currently sees. It is the strongest signal that Claude was deciding on stale data — check the session log around the same timestamp for the action that desynced state.

For hands-on primitive debugging, run `NO_CLAUDE=1 mineclaude` and use the **Console** panel in the monitor frontend. You type the same code Claude would put in `newAction` (e.g. `await goToPosition(0, 64, 0)`), it enqueues on the same action queue, and the resulting trace renders in the Action Queue panel with full subaction breakdown. Useful for reproducing "Claude did X and something weird happened" without Claude in the loop.

E2E tests live in `tests/e2e/` and are opt-in: `pytest --run-e2e`.

## Known Workarounds

- **Signed chat crash:** `ONLINE_MODE=false` breaks MC signed chat on 2nd+ outgoing message. `ChatRoute.kt` sends plain text via `sendChatCommand("tellraw @a {\"text\":…}")` instead. Slash-commands and `#`/`\` prefixes go through `sendChatMessage`/`sendChatCommand` so Baritone's chat hook intercepts them client-side
- **Emojis:** MC can't render them — `ChatRoute` strips non-ASCII before wrapping in `/tellraw`. Prompt tells Claude not to use them
- **Bot opping:** `OPS` env var unreliable with offline-mode. `entrypoint.sh` RCONs `op Claude` + `op Massimino` after connection, plus `gamerule doImmediateRespawn true`
- **Baritone nav timeout:** the agent's `navigate_near` primitive uses a 15s deadline. Longer waits on unreachable targets (tree-top logs behind leaves, walled-off ore) just burn Claude iterations on guaranteed failures
