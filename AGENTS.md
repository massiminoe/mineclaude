# Mineclaude

Mineclaude is a headless Minecraft bot runtime driven by an external MCP agent.
The runtime has no built-in LLM. Read [CLAUDE.md](CLAUDE.md) for the complete
bridge API and operational gotchas; keep this file concise and durable.

## Architecture

- `mineclaude/` is the Python runtime: bridge client, sandboxed primitives,
  single-flight action queue, reflexes, game state, MCP server, monitor, and
  session logging.
- `mc-mod/` is the Kotlin/Fabric native bridge. Any Minecraft read or mutation
  must execute on the client tick thread via `TickThread.submitAndWait`.
- `frontend/` is the React/Vite monitor. `skills/mineclaude/` documents the MCP
  driving primitives; regenerate generated skill docs with `make skill-docs`.
- `bench/` measures advancements earned by a harness/model pair in a fixed-seed
  survival world. It stores artifacts under `state/bench/` locally or S3 on AWS.

## Development

- Use Python 3.13 and `.venv`. Run unit tests with `make test`; use
  `make test-e2e` only when intentionally starting the Docker E2E stack.
- Start the normal stack with `docker compose up --build`, the runtime with
  `make run`, and the frontend with `make frontend`.
- Preserve the single-flight action invariant: MCP and monitor must share one
  `Runtime`; only one bridge-driving action may run at a time.
- Keep `.env`, auth files, generated runtime state, videos, and session logs out
  of commits. Do not weaken the sandbox AST validation to make an agent task fit.

## AWS benchmark access

- Use `AWS_PROFILE=mineclaude-sso` for AWS benchmark commands and authenticate
  with `aws sso login --profile mineclaude-sso`. Do not use the default
  `aws login` profile for benchmark work.
- Codex auth belongs only in encrypted AWS SSM worker slots. Never print, commit,
  or include auth JSON in benchmark artifacts.
- Pass `--codex-worker N` to one launch; a four-worker sweep uses
  `--codex-workers 1,2,3,4 --concurrency 4`. Current slots originated from one
  Codex login snapshot, so run a short parallel pilot before long parallel runs.
