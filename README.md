# Mineclaude

Mineclaude lets an external agent play Minecraft through **MCP**. It provides a
headless Minecraft client, a native Kotlin/Fabric bridge, a Python runtime, and a
read-only browser monitor. The runtime has no built-in LLM; the connected agent
supplies the decisions.

The [benchmark](bench/README.md) measures how many Minecraft advancements a
**model + harness** earns in a fresh, fixed-seed survival world within a fixed
time budget. It records advancement events, transcripts, runtime logs, usage
where available, and gameplay video. Claude Code, OpenCode, Cursor, and Codex
harnesses are included.

## Quickstart

Requirements: Python 3.13, Node.js 22.12+ (for the monitor), Git, and a running
Docker Engine/Desktop with Compose. The Docker build downloads Minecraft and
the required mods; you do not need to install Java or Gradle on the host.
The Compose configuration accepts the Minecraft EULA for the server.

```bash
git clone https://github.com/massiminoe/mineclaude.git
cd mineclaude
python3.13 -m venv .venv
.venv/bin/python -m pip install -c requirements-dev.lock -e '.[dev]'
cd frontend && npm ci && cd ..
```

Run these in separate terminals from the repository root:

```bash
# Minecraft server + headless client + bridge
docker compose up --build

# Python runtime: MCP on 5556, monitor on 5555
make run

# Monitor development server on 5173
make frontend
```

Open [the monitor](http://localhost:5173). The first world/client startup can
take several minutes. The monitor shows video, actions, events, and inventory;
actions come from the connected MCP agent.

Connect your agent to `http://127.0.0.1:5556/mcp` and give it the
[Mineclaude skill](skills/mineclaude/SKILL.md). For Claude Code:

```bash
claude mcp add --transport http mineclaude http://127.0.0.1:5556/mcp
```

The runtime requires no provider credentials. Your external agent or benchmark
harness authenticates separately. Optional runtime settings are documented in
[.env.example](.env.example); copy it to `.env` if you need overrides.

On Apple Silicon, the default client runs under amd64 emulation. `make up-arm`
uses the separate native arm64 build. Benchmark comparisons should record which
platform was used.

These services are intended for a trusted development environment. The bridge
and monitor have no authentication, and the bundled Minecraft server uses
offline mode. Keep their ports on a trusted network.

## How it works

The agent calls MCP tools to inspect the world, take screenshots, and execute
short Python actions in the runtime's restricted primitive environment. The
native bridge performs Minecraft operations on the client tick thread.
Baritone assists navigation and mining; this is a structured-tool benchmark,
not a keyboard-and-mouse-only benchmark. One shared runtime enforces a single
bridge-driving action at a time and supplies hazard reflexes.

The normal development world is peaceful. The benchmark overlay uses normal
difficulty, a fixed seed, and modified game rules including `keepInventory`.
See [benchmark methodology](bench/README.md#methodology-and-score-boundaries)
before interpreting scores.

## Development and checks

```bash
make test             # unit tests; no Minecraft stack
make frontend-build   # TypeScript check + production build
make skill-docs       # regenerate primitive, event, and tool documentation
make run-mock         # runtime with mock bridge; no Minecraft needed
```

After `make frontend-build`, the runtime serves the monitor at
[localhost:5555](http://localhost:5555) without the Vite development server.
`make test-e2e` builds a temporary Docker project, crafts through MCP, checks
the monitor and screenshot, then removes only its own containers and volumes.
It needs Docker Compose 2.24.4+ and no model credentials. On Apple Silicon it
uses the native arm64 client. Diagnostic logs are kept under `state/e2e/`. `docker compose down` stops the development stack;
`docker compose down -v` also removes its named volumes.

## Repository guide

- [bench/](bench/README.md): run orchestration, AWS sweeps, scoring, and analysis.
- [mineclaude/](mineclaude/): Python runtime, MCP server, monitor, and sandbox.
- [mc-mod/](mc-mod/): Kotlin/Fabric native bridge.
- [mc-client/](mc-client/): headless client images and startup scripts.
- [frontend/](frontend/README.md): React monitor.
- [skills/mineclaude/](skills/mineclaude/SKILL.md): agent-facing driving instructions.
- [CLAUDE.md](CLAUDE.md): detailed bridge API and operational gotchas.
- [RELEASING.md](RELEASING.md): release preparation and reproducibility checks.
- [THIRD_PARTY.md](THIRD_PARTY.md): dependency and asset provenance.

Run artifacts, credentials, and session state belong under ignored local paths,
not in Git. The code release and benchmark dataset are separate deliverables.

## License

Mineclaude's original code is available under the [MIT License](LICENSE).
Third-party code and assets retain their own terms; see [THIRD_PARTY.md](THIRD_PARTY.md).
The code license does not cover Minecraft assets or the separately published
benchmark dataset and gameplay recordings.
