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
- **Determinism**: fixed world seed (`BENCH_SEED`, default
  `mineclaude-bench-1`), fixed MC version (1.21.5), difficulty `normal`,
  pristine world per run. Mob/weather RNG remains; average over trials.

## Anatomy of a run

`bench/compose.bench.yml` overlays the base docker-compose stack:

    mc-server (fixed seed) <- mc-client (bridge mod) <- mineclaude (MCP) <- harness (Claude Code)

`bench/run.sh` orchestrates one trial: build, world up, wait until the bot is
in-world, start the harness (the clock starts here), let the harness self-exit
at the budget, snapshot `GET /advancements`, score with `bench/score.py`, and
collect everything into `state/bench/<run-id>/`:

    metadata.json      run id, harness, model, seed, git sha, t0
    score.json         earned count + chronological breakdown
    usage.json         token ledger + cost + rate-limit health (bench/usage.py)
    advancements.json  raw ledger snapshot (ground truth)
    harness/           Claude Code stream-json transcripts + harness log
    sessions/          mineclaude session log (advancement receipt timestamps)
    video/             full gameplay recording (15 fps, ~70-90 MB per 30 min,
                       so ~150-180 MB for the 1h default budget)
    logs.txt           compose logs
    bench-perf.log     15s samples of VM load / container CPU (cloud runs only)

## Token cost and throttled trials

`bench/usage.py` folds the harness transcripts into `usage.json` so cost analysis never
has to re-download hundreds of MB of `claude-*.jsonl`. It records input / output /
thinking / cache-write (split 1h vs 5m) / cache-read tokens, turns, per-model
`modelUsage`, and `cost_usd`.

Two things about the source format that the code depends on, both verified rather than
assumed:

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

Re-derive it for any run from its artifacts:

    python3 bench/usage.py --harness <run>/harness --out <run>/usage.json

## Analysis

`bench/analysis/advancement_curves.ipynb` — cumulative advancements over time per model,
mean + min/max spread, pacing (how much is banked by the halfway mark, how long each trial
ran silent), and cost per advancement. Reads `state/bench/sweep-*/` directly; no live
stack needed. Run it with the repo venv: `.venv/bin/jupyter lab`.

The harness container (`harness/claude-code/`) holds the harness *contract*:
it gets `MCP_URL`, `BENCH_MODEL`, `BENCH_RUN_SECONDS`, auth env, read-only
`/skills` + `/scoring` mounts and an `/artifacts` mount, and must exit by
itself when the budget ends. Any future harness (other CLIs, raw API loops)
is a new image honoring the same contract.

## Auth (Claude Code harness)

The harness authenticates with a long-lived subscription token:

    claude setup-token          # prints a token minted from your Claude plan
    # put it in the repo .env (gitignored):
    echo 'CLAUDE_CODE_OAUTH_TOKEN=<token>' >> .env

An `ANTHROPIC_API_KEY` works too (per-token billing instead).

## Local run (dev)

    bench/run.sh --local --seconds 600 --model claude-haiku-4-5-20251001

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
