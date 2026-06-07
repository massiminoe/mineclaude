# Mineclaude

A headless Minecraft bot **runtime**, driven over **MCP** by an external agent
(e.g. Claude Code) through a native Kotlin bridge mod. A React frontend lets you
watch (and manually drive) the bot. There is no built-in LLM — the driving agent
is external, so no API keys are needed.

There are three pieces you run: the **Docker stack** (MC server + headless client +
bridge), the **runtime** (MCP server + monitor), and the **frontend** monitor.

## Setup

1. Python 3.13+. Use the virtualenv:
   ```bash
   python3.13 -m venv .venv && source .venv/bin/activate
   pip install -e .
   ```
2. (Optional) copy `.env.example` to `.env` to override the bridge / MCP / monitor
   defaults. No API keys required.

## Run it

```bash
# 1. Start MC server + headless client + bridge mod (HTTP :8081, events WS :8082)
docker compose up --build

# 2. Start the runtime — MCP server (:5556) + monitor (:5555), no API key
mineclaude

# 3. Watch / drive it in the browser (dev server on :5173)
cd frontend && npm install && npm run dev
```

Connect Claude Code to drive the bot:

```bash
claude mcp add --transport http mineclaude http://127.0.0.1:5556/mcp
```

The monitor UI runs inside the runtime process on port **5555**; the frontend dev
server proxies `/api` to it.

## Useful variants

| Command | What it does |
| --- | --- |
| `MOCK_BRIDGE=1 mineclaude` | Run with no MC server (in-memory mock bridge) |
| `SESSION_LOG=0 mineclaude` | Disable the per-run session JSONL log |
| `docker compose down -v` | Full clean restart (clears volumes, regenerates ops) |
| `docker compose logs mc-client` | Tail bridge / MC logs |
| `cd frontend && npm run build` | Production frontend build (served by the monitor) |

You can also drive primitives by hand from the frontend **Console** panel without
any external agent connected.

## Tests

```bash
pytest               # unit tests
pytest --run-e2e     # include opt-in end-to-end tests (tests/e2e/)
```

## Where to look

- `mineclaude/` — Python package (bridge, sandbox, primitives, action_queue, reflexes, runtime, gamestate, models, mcp_server, monitor, session_log, main)
- `skills/mineclaude/` — the Claude Code skill: how to drive the bot over MCP
- `frontend/` — React + TypeScript + Vite monitor UI
- `mc-mod/` — Kotlin Fabric bridge mod (all bridge HTTP/WS endpoints)
- `mc-client/` — Dockerfile + entrypoint for the headless MC client
- `CLAUDE.md` — full architecture notes, bridge API reference, and gotchas
