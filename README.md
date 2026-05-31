# Mineclaude

A Minecraft bot: a Python agent that uses Claude to control a headless MC client
through a native Kotlin bridge mod. A React frontend lets you watch (and manually
drive) the bot.

There are three pieces you run: the **Docker stack** (MC server + headless client +
bridge), the **agent**, and the **frontend** monitor.

## Setup

1. Python 3.13+. Use the virtualenv:
   ```bash
   python3.13 -m venv .venv && source .venv/bin/activate
   pip install -e .
   ```
2. Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`.

## Run it

```bash
# 1. Start MC server + headless client + bridge mod (HTTP :8081, events WS :8082)
docker compose up --build

# 2. Start the agent (needs .env with ANTHROPIC_API_KEY)
mineclaude

# 3. Watch / drive it in the browser (dev server on :5173)
cd frontend && npm install && npm run dev
```

The monitor UI runs inside the agent process on port **5555**; the frontend dev
server proxies `/api` to it.

## Useful variants

| Command | What it does |
| --- | --- |
| `MOCK_BRIDGE=1 mineclaude` | Run the agent loop with no MC server (mock bridge) |
| `NO_CLAUDE=1 mineclaude` | No Claude — queue + bridge + monitor stay up so you can drive primitives by hand from the frontend **Console** panel |
| `docker compose down -v` | Full clean restart (clears volumes, regenerates ops) |
| `docker compose logs mc-client` | Tail bridge / MC logs |
| `cd frontend && npm run build` | Production frontend build (served by the monitor) |

## Tests

```bash
pytest               # unit tests
pytest --run-e2e     # include opt-in end-to-end tests (tests/e2e/)
```

## Where to look

- `agent/` — Python package (bridge, sandbox, primitives, claude, agent, prompt, main, monitor)
- `frontend/` — React + TypeScript + Vite monitor UI
- `mc-mod/` — Kotlin Fabric bridge mod (all bridge HTTP/WS endpoints)
- `mc-client/` — Dockerfile + entrypoint for the headless MC client
- `CLAUDE.md` — full architecture notes, bridge API reference, and gotchas
