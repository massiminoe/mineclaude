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
- `POST /place`, `/break`, `/craft`, `/smelt`, `/equip`, `/discard` — MVP via server commands and container APIs
- `POST /collect` `{radius}` — walk to and pick up dropped item entities within radius of player
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
- `minescript.entities()` returns `List[EntityData]` — access `.position`, `.name`, `.type`, `.health`. **`ent.type` format is `"entity.minecraft.zombie"`** (with dots) NOT `"minecraft:zombie"` (with colon). Use `_clean_entity_type()` in `minescript_api.py` to strip — `.replace("minecraft:", "")` is a no-op
- `minescript.player_inventory()` returns `List[ItemStack]` — access `.item`, `.count`, `.slot`
- `minescript.world_info()` returns `WorldInfo` dataclass — use `.day_ticks`, `.raining`, etc.
- `minescript.player_biome()` does NOT exist in v5.0
- Chat events: use `EventQueue` + `register_chat_listener()`, NOT `chat_events()`
- `ChatEvent` may arrive as dict — check `isinstance(ev, dict)` before `hasattr(ev, "message")`
- Chat messages include `[Not Secure]` prefix with `ONLINE_MODE=false` — use regex to find `<Player>` pattern
- Player-control APIs take `pressed: bool` arg: `player_press_attack(True/False)`, `player_press_use(True/False)`, etc.
- `player_look_at(x, y, z)` exists — use it instead of manual yaw/pitch math
- `player_inventory_select_slot(slot)` — selects hotbar slot (NOT `player_select_slot`)
- `player_inventory_slot_to_hotbar(slot)` — exists but BROKEN on MC 1.21.5 (ServerboundPickItemPacket removed in 1.21.4). Workaround: `/item replace` commands (see player_control.py)
- Container APIs (from custom build with PR #40): `container_open(x,y,z)`, `container_close()`, `container_click_slot(slot,button,shift)`, `container_swap_slots(slot1,slot2)`, `container_get_items()`, `container_get_slot(slot)`, `container_get_info()`, `container_find_item(item_id)`
- `GET /probe` endpoint — returns JSON of all available Minescript APIs and capabilities

## Known Workarounds

- **Signed chat crash**: `ONLINE_MODE=false` breaks MC signed chat on 2nd+ message. Bot sends via `/tellraw @a` instead of `minescript.chat()`
- **Emojis**: MC can't render them — stripped to ASCII before sending, prompt tells Claude not to use them
- **Bot opping**: `OPS` env var unreliable with offline-mode. RCON ops both bot and player in entrypoint after connection
- **Bridge logging**: Must NOT use Python `logging` to stdout (Minescript routes it to MC chat → feedback loop). Logs to `/tmp/bridge.log`
- **break/place/attack**: Real player actions (look_at + press_attack/press_use). Verified working in-game. Break does NOT auto-collect drops — agent must call `collectItems()` after. **place** verifies via `getblock` after `press_use`: confirmed-placed → success, verify-errored → tolerant success, still-air → honest `{"placed": False, "error": "...is a GUI open?"}`. **All world-interaction primitives** (`_place_real`, `_break_real`, `_attack_real`, `_discard_real`) call `_ensure_no_screen_open()` defensively at the top — input is captured by Screens, so any lingering inventory GUI would silently no-op the action
- **collect (item pickup)**: MC requires walking within ~1 block of dropped item entities. `collect_nearby_items(radius)` in `player_control.py` scans `minescript.entities()` for `entity.minecraft.item` types within radius of player, walks to each via Baritone `#goto`. Idempotent — returns 0 (success) when nothing nearby. 18s overall time budget, max 4 iterations
- **discard**: Real (select slot + press_drop). Works if item already in hotbar
- **equip hand/offhand**: Real (inventory_select_slot / swap_hands). Works if item in hotbar
- **equip armor**: Real — opens player inventory screen via `press_key_bind('key.inventory', True/False)` (the fork lacks `player_press_inventory`), finds armor in inventory portion (slots 9-44 of `InventoryMenu`), uses `container_swap_slots(source, armor_slot)` to move it. Armor slot indices in `InventoryMenu`: head=5, chest=6, legs=7, feet=8. Fallback `/item replace entity @s armor.{slot}` retained as defensive backstop. Verified end-to-end with iron_helmet
- **craft**: Real — opens a crafting menu via container APIs and clicks ingredients into the grid. 3x3 recipes use a nearby `crafting_table` block via `container_open(x,y,z)`. 2x2 recipes use the player's built-in 2x2 crafter via `press_key_bind('key.inventory')` (the fork lacks `player_press_inventory`/`open_inventory`). Click model per ingredient: **left-click source** to pick up entire stack, **right-click grid slot** to drop 1 from cursor, **left-click source** to drop cursor stack back (re-stacks since same item) — leaves cursor empty between placements so the same source can feed multiple grid slots. Shift-click slot 0 to extract output. Cleanup phase shift-clicks any leftover grid items back to inventory before closing (otherwise MC drops them as entities). Slot layouts: `CraftingMenu` (table) reports `player_slots=36, container_slots=10` with player inv at slots 10-45; `InventoryMenu` (E key) reports `player_slots=41, container_slots=5` with armor at 5-8, player inv at 9-44, offhand at 45. Both screens have title `"Crafting"` — distinguish by container_id/slot count if needed. **Both open and close are verified** via `_is_any_screen_open()` (uses `screen_name()` then `container_get_info()`) with one retry on failure — close failures previously left the inventory open which then silently no-op'd subsequent world actions. **All inventory clicks are paced to whole game ticks** via `_tick_sleep(n)` (`MC_TICK_MS = 50`) — gives MC time to process each event between calls and makes the agent's actions visibly human in the game window
- **press_key_bind**: This Minescript fork's substitute for the missing `player_press_inventory`/`open_inventory` APIs. `press_key_bind("key.inventory", True/False)` works to **OPEN** the player inventory (when no screen is active) but does NOT work to close it. Reason: MC's `Minecraft.tick()` only calls `handleKeybinds()` when `screen == null`, so global keybind events are queued but never processed while a screen is open. Worse — the queued click would be consumed by the next `handleKeybinds()` call after a successful close via another path, **immediately re-opening the inventory**. So `_try_close_once()` deliberately avoids `press_key_bind` and uses `container_close` instead (which calls `LocalPlayer.closeContainer()` regardless of screen state). Tick-paced: 1 tick between keydown and keyup, 2 ticks settle after release
- **smelt**: Real furnace smelting via container APIs + /item replace block — opens furnace, inserts items, polls lit state, extracts output
- **Hotbar movement**: Uses `/item replace ... from` commands (lossless, preserves NBT/durability) since `player_inventory_slot_to_hotbar` is broken on MC 1.21.5. Prefers empty hotbar slots; if full, uses a temp inventory slot for swap
- **Custom Minescript build**: JAR built from `massiminoe/minescript@mc1.21.5-containers` (5.0b11 + container APIs from PR #40)
- The `method` field in response dicts indicates "real" or "fallback" path
