# Mineclaude Bench

A benchmark for LLM agents + harnesses: **how many Minecraft advancements can
an agent earn in a fixed wall-clock budget**, driving the mineclaude bot in a
fresh, fixed-seed survival world?

- **Metric**: the **count** of advancements earned within the budget (default
  budget: 3600s / 1 hour). Every advancement counts 1.
  Per-advancement timestamps are captured too, so time-based metrics (AUC,
  time-to-milestone) can be derived later from the same artifacts.
- **Gamerscore (weighted points) is currently DISABLED**, but the logic is
  kept: `scoring/gamerscore.json` maps all 122 Java 1.21.5 display
  advancements to points (Bedrock gamerscore where an equivalent achievement
  exists — 47 of them — gamerscore-convention values for the rest; 3,375 max),
  and any run can be re-scored offline from its artifacts:

      python3 bench/score.py --advancements <run>/advancements.json \
          --gamerscore --scoring bench/scoring/gamerscore.json
- **A benchmark entry** is (harness, model): e.g. `claude-code` +
  `claude-haiku-4-5` and `claude-code` + `claude-sonnet-5` are two entries.
  Three harnesses ship today — `claude-code`, `opencode`, `cursor` — selected
  with `--harness` on `run.sh` / `aws/launch.sh` / `aws/sweep.sh`.
- **Determinism**: fixed world seed (`BENCH_SEED`, default
  `mineclaude-bench-1`), fixed MC version (1.21.5), difficulty `normal`,
  pristine world per run. Mob/weather RNG remains; average over trials.

## Anatomy of a run

`bench/compose.bench.yml` overlays the base docker-compose stack:

    mc-server (fixed seed) <- mc-client (bridge mod) <- mineclaude (MCP) <- harness

`bench/run.sh` orchestrates one trial: build, world up, wait until the bot is
in-world, start the harness (the clock starts here), let the harness self-exit
at the budget, snapshot `GET /advancements`, score with `bench/score.py`, and
collect everything into `state/bench/<run-id>/`:

    metadata.json      run id, harness, model, seed, git sha, t0
    score.json         earned count + chronological breakdown
    usage.json         token ledger + cost + rate-limit health (bench/usage.py)
    advancements.json  raw ledger snapshot (ground truth)
    harness/           the harness's own transcripts + harness log
    sessions/          mineclaude session log (advancement receipt timestamps)
    video/             full gameplay recording (15 fps, ~70-90 MB per 30 min,
                       so ~150-180 MB for the 1h default budget)
    logs.txt           compose logs
    bench-perf.log     15s samples of VM load / container CPU (cloud runs only)

## Token cost and throttled trials

`bench/usage.py` folds the harness transcripts into `usage.json` so cost analysis never
has to re-download hundreds of MB of transcript. It records input / output /
thinking / cache-write (split 1h vs 5m) / cache-read tokens, turns, per-model
`modelUsage`, and `cost_usd`. One schema, one parser per harness — picked from the
run's `metadata.json`, so every pre-existing run parses exactly as before — and
`cost_basis` names what the cost figure actually is (`list_price_estimate`,
`gateway_metered`, `cursor_raw_cost`, or `unavailable` when the harness cannot
report one — in which case `tokens` and `cost_usd` are `null` rather than zero,
so a run with no ledger is never mistaken for a free one).

Rate-limit detection for the two new harnesses is scoped to **error payloads and
stderr only**, never the transcript body. A transcript carries every tool result
the agent saw, and a loose pattern quarantines valid trials: the first pilot was
wrongly flagged `THROTTLED` because the mineclaude skill has a line numbered 429
and because epoch timestamps like `1788054294` contain those digits.

Two things about the Claude Code source format that the code depends on, both verified
rather than assumed:

- **Only the `result` event is authoritative.** The per-message `usage` blocks on
  `assistant` events are mid-stream snapshots — summing them overcounted cache reads by
  60% and undercounted output by 40x on a sample run.
- **Each invocation's `result` is its own**, not cumulative, even though the harness loop
  re-enters with `--continue` under one session id. Summing across `claude-N.jsonl` is
  correct.

`cost_usd` is the CLI's **list-price** computation. Runs authenticate with a subscription
token, which is not billed per token, so it is an equivalent rather than an invoice; the
token counts are real either way.

`health.throttled` is the quarantine flag: it goes true when the harness got a hard
`rejected` rate-limit event, meaning that trial measured the rate limiter and not the
model. `sweep.sh` prints such a trial but holds it out of the mean and the hit-rate, and
`bench/analysis/advancement_curves.ipynb` drops it from every chart. Keep the artifacts —
quarantine, don't delete. (Backfilled onto the Aug 2026 runs: one fable-5 trial,
`20260817-080733-t3`, is throttled.)

Re-derive it for any run from its artifacts (`--kind` only when there is no
metadata.json to read it from):

    python3 bench/usage.py --harness <run>/harness --out <run>/usage.json

## Analysis

`bench/analysis/advancement_curves.ipynb` — cumulative advancements over time per model,
mean + min/max spread, pacing (how much is banked by the halfway mark, how long each trial
ran silent), and cost per advancement. Reads `state/bench/sweep-*/` directly; no live
stack needed. Run it with the repo venv: `.venv/bin/jupyter lab`.

## Harnesses

`bench/harness/` holds one image per harness plus the shared contract they all
implement (`common.sh`, `prompt.md` — the build context is `bench/harness/`, so
every image gets the same task text and the same MCP-readiness gate). A harness
gets `MCP_URL`, `BENCH_MODEL`, `BENCH_RUN_SECONDS`, its auth env, read-only
`/skills` + `/scoring` mounts and an `/artifacts` mount, and must exit by itself
when the budget ends.

| harness | driver | skill path | auth | usage source |
|---|---|---|---|---|
| `claude-code` | `claude -p --continue`, stream-json | `.claude/skills/` | `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` | `result` events (list price) |
| `opencode` | `opencode run --format json --auto` | `.claude/skills/` (native Claude-compatible path) | `OPENCODE_API_KEY` | `step_finish` events (Zen metered) |
| `cursor` | `@cursor/sdk` local agent (`driver.mjs`) | `.cursor/skills/` | `CURSOR_API_KEY` | none on a Pro plan — **see below** |

Model ids carry their provider where the CLI expects it:

    bench/run.sh --harness opencode --model opencode-go/qwen3.8-flash
    bench/run.sh --harness cursor   --model composer-2.5

The five opencode Go models the matrix targets — `deepseek-v4-flash-vision-exp`,
`glm-5.3-flash`, `gpt-5.6-luna`, `qwen3.8-flash`, `qwen3.8-max` — all support
tool calls *and* image attachments, which the `screenshot` MCP tool needs. On
Cursor, `composer-2.5` and `grok-4.6`; the run dumps the account's live
catalogue to `harness/cursor-models.json` so a renamed id is visible rather than
mysterious.

**Cursor runs have no token or cost ledger.** Measured, not assumed. The CLI's
stream-json result event carries only `duration_ms`. `@cursor/sdk` declares
`agent.getUsage()` (returning `rawCostCents` / `chargedCents` plus full token
counts), which is why the harness drives the SDK — but that call is
**entitlement-gated**: on an individual Pro account it returns
`[feature_unavailable] This feature is not available for your account`, and the
event stream carries no `usage` events either (only `status`, `thinking`,
`assistant`, `tool_call`). On Pro there is no programmatic per-run usage from
Cursor by any route.

`usage.py` reports that honestly — `tokens: null`, `cost_usd: null`,
`cost_basis: "unavailable"`, and `health.usage_error` naming the reason. It must
never be zeros: a run with 100+ tool calls reporting `total_tokens: 0` reads as a
free run and drags any mean toward zero. `turns` (assistant messages) and
`tool_calls` still land, so Cursor entries compare on advancements and activity
and drop out of cost-per-advancement charts.

The SDK remains the better driver on its own merits — MCP servers passed inline
(no config file to go missing), a structured event stream, programmatic
deadline/cancel — and if `getUsage()` ever becomes available (a Teams seat is the
likely unlock) the ledger populates with no code change. The CLI stays the
fallback: same entrypoint contract.

**Adding a harness** is a directory: `bench/harness/<name>/Dockerfile` +
`entrypoint.sh` that sources `/opt/bench/common.sh`, writes transcripts to
`/artifacts`, and self-exits at the deadline; a parser in `bench/usage.py`; and
a credential case in `run.sh`, `aws/setup.sh`, and `aws/user-data.sh.tpl`.

### Budget and quota, before you run a sweep

A measured 1h Claude Code trial burns 16–48M tokens, overwhelmingly cache reads.
Both new subscriptions meter in dollars, so that shape matters more than the
model's sticker price:

- **opencode Go** — $12 per 5h, $30/week, $60/month at Zen list prices. The
  flash-tier models are cheap even at that volume; `qwen3.8-max` ($2/$6 per Mtok)
  is only viable if prompt caching is active through the gateway, which is
  exactly what `usage.json`'s `cache_read` column tells you.
- **Cursor Pro** — a $20/month credit pool, i.e. roughly one to two hour-long
  runs at frontier prices. Run `composer-2.5` first. This cannot be tracked from
  the artifacts (see above) — watch the dashboard.

Measured on the first 10-minute pilots (local, arm64): `qwen3.8-flash` spent
**$0.031**, with caching confirmed active through the gateway (867k cache-read
against 156 raw input tokens) — roughly $0.20 for a 1h trial. The flash tier is
nowhere near the Go caps; only `qwen3.8-max` (~15x the cache-read rate) is worth
watching. opencode's `step_finish` token counts were verified per-step, not
cumulative, against a real transcript.

Pilot each new (harness, model) at `--seconds 600` and read `usage.json` before
committing to 1h trials.

### MCP tool timeout

opencode and Cursor both drive MCP through the TypeScript SDK, whose tool-call
timeout defaults to 60s. `execute`'s inline wait is 50s, so `run.sh` drops it to
40s (`BENCH_EXECUTE_WAIT_S` -> the mineclaude service's
`MINECLAUDE_EXECUTE_WAIT_S`) for non-Claude harnesses: a long action then
backgrounds as `status:"running"` instead of failing client-side.

## Auth

The harness authenticates with a long-lived subscription token:

    claude setup-token          # prints a token minted from your Claude plan
    # put it in the repo .env (gitignored):
    echo 'CLAUDE_CODE_OAUTH_TOKEN=<token>' >> .env

An `ANTHROPIC_API_KEY` works too (per-token billing instead).

The other harnesses take a single key each, also from the repo `.env`:

    OPENCODE_API_KEY=...   # opencode Zen -> subscribe to Go -> copy the key
    CURSOR_API_KEY=...     # Cursor dashboard -> API Keys

`bench/run.sh` checks only the credential its `--harness` needs, before world-gen
burns ten minutes. `bench/aws/setup.sh` uploads whichever are present to SSM
(`/mineclaude-bench/{claude-code-oauth-token,opencode-api-key,cursor-api-key}`),
and each VM pulls only its own.

## Local run (dev)

    bench/run.sh --local --seconds 600 --model claude-haiku-4-5-20251001
    bench/run.sh --local --seconds 600 --harness opencode --model opencode-go/qwen3.8-flash

`--local` swaps in the native arm64 mc-client (Apple Silicon). `--keep` leaves
the stack up afterwards; the monitor stays at http://localhost:5555 during a
run. Local runs and the normal dev workflow don't conflict as long as only one
stack is up at a time (they share host ports).

## Cloud run (AWS, ephemeral VM per run)

One-time: `aws configure`, then

    bench/aws/setup.sh          # bucket, IAM role, security group, key pair,
                                # and uploads CLAUDE_CODE_OAUTH_TOKEN to SSM

Per run (push your branch first — the VM clones from GitHub):

    bench/aws/launch.sh --seconds 3600 --model claude-sonnet-5 [--spot]
    bench/aws/launch.sh --seconds 3600 --harness cursor --model composer-2.5

This boots a c7i.2xlarge (8 vCPU/16 GB, ~$0.36/hr on-demand, ~$0.14 spot; the
whole stack runs native amd64 — no emulation), executes the run, uploads
artifacts to `s3://mineclaude-bench-<account>/runs/<run-id>/`, and
self-terminates (with a deadman `shutdown` in case anything wedges). The
launcher waits and prints the score; `--no-wait` to fire-and-forget. Debug a
live VM with the printed ssh command; boot log is `/var/log/bench-userdata.log`.

## Roadmap

- Parallelism: N ≤ 4 independent runs = N `launch.sh` invocations (fully
  independent VMs already); a small sweep driver + results table comes next.
- Weighted gamerscore is parked, not deleted — re-enable by passing
  `--gamerscore --scoring …` in `run.sh` and re-surfacing the table to the
  agent in `harness/claude-code/entrypoint.sh` + `prompt.md`. Bump
  `scoring/gamerscore.json`'s `version` on any change; scores only compare
  within (seed, metric, budget).
