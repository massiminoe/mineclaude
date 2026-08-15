# Mineclaude Bench

A benchmark for LLM agents + harnesses: **how much Xbox-style gamerscore can an
agent earn in a fixed wall-clock budget**, driving the mineclaude bot in a
fresh, fixed-seed survival world?

- **Metric**: sum of points for advancements earned within the budget.
  `scoring/gamerscore.json` maps all 122 Java 1.21.5 display advancements to
  points (Bedrock-edition gamerscore where an equivalent achievement exists —
  47 of them — gamerscore-convention values for the rest; 3,375 points max).
  Per-advancement timestamps are captured too, so time-based metrics (AUC,
  time-to-milestone) can be derived later from the same artifacts.
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
    score.json         total points + chronological breakdown
    advancements.json  raw ledger snapshot (ground truth)
    harness/           Claude Code stream-json transcripts + harness log
    sessions/          mineclaude session log (advancement receipt timestamps)
    video/             full gameplay recording (~25-50 MB per 30 min)
    logs.txt           compose logs

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

    bench/aws/launch.sh --seconds 1800 --model claude-haiku-4-5-20251001 [--spot]

This boots a c7i.2xlarge (8 vCPU/16 GB, ~$0.36/hr on-demand, ~$0.14 spot; the
whole stack runs native amd64 — no emulation), executes the run, uploads
artifacts to `s3://mineclaude-bench-<account>/runs/<run-id>/`, and
self-terminates (with a deadman `shutdown` in case anything wedges). The
launcher waits and prints the score; `--no-wait` to fire-and-forget. Debug a
live VM with the printed ssh command; boot log is `/var/log/bench-userdata.log`.

## Roadmap

- Parallelism: N ≤ 4 independent runs = N `launch.sh` invocations (fully
  independent VMs already); a small sweep driver + results table comes next.
- Scoring is v1 — bump `scoring/gamerscore.json`'s `version` on any change;
  scores only compare within (seed, scoring version, budget).
