#!/usr/bin/env bash
# Run ONE scored benchmark trial end-to-end on this machine:
#   world up (fixed seed) -> wait until the bot is in-world -> start the
#   harness (clock starts) -> harness self-exits at the budget -> snapshot the
#   advancement ledger -> score -> collect artifacts -> tear down.
#
# Usage:
#   bench/run.sh [--seconds 3600] [--harness <name>] [--model <id>]
#                [--run-id <id>] [--seed <s>] [--local] [--keep]
#   --harness  which bench/harness/<name> image drives the run
#              (claude-code | opencode | cursor); default claude-code
#   --local    use the native arm64 mc-client (Apple Silicon dev)
#   --keep     leave the stack up after the run (debugging)
#
# A benchmark entry is (harness, model) — e.g. opencode + opencode-go/qwen3.8-max.
# Auth for the harness comes from the environment (or repo .env, which compose
# reads); which variable is required depends on --harness:
#   claude-code  CLAUDE_CODE_OAUTH_TOKEN (claude setup-token) or ANTHROPIC_API_KEY
#   opencode     OPENCODE_API_KEY  (opencode Zen / Go)
#   cursor       CURSOR_API_KEY    (Cursor dashboard -> API Keys)
set -euo pipefail
cd "$(dirname "$0")/.."

SECONDS_BUDGET=3600
HARNESS="claude-code"
MODEL="claude-haiku-4-5-20251001"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
SEED="mineclaude-bench-1"
LOCAL=0
KEEP=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --seconds) SECONDS_BUDGET="$2"; shift 2 ;;
        --harness) HARNESS="$2"; shift 2 ;;
        --model)   MODEL="$2"; shift 2 ;;
        --run-id)  RUN_ID="$2"; shift 2 ;;
        --seed)    SEED="$2"; shift 2 ;;
        --local)   LOCAL=1; shift ;;
        --keep)    KEEP=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ ! -f "bench/harness/${HARNESS}/Dockerfile" ]]; then
    echo "bench: unknown harness '$HARNESS' (no bench/harness/$HARNESS/Dockerfile)" >&2
    echo "       available: $(ls -d bench/harness/*/ | xargs -n1 basename | tr '\n' ' ')" >&2
    exit 2
fi

# Each harness authenticates differently; check its own credential up front
# rather than discovering the gap after world-gen has burned ten minutes.
case "$HARNESS" in
    claude-code) AUTH_VARS="CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY" ;;
    opencode)    AUTH_VARS="OPENCODE_API_KEY" ;;
    cursor)      AUTH_VARS="CURSOR_API_KEY" ;;
    *)           AUTH_VARS="" ;;
esac
if [[ -n "$AUTH_VARS" ]]; then
    have_auth=0
    for v in ${AUTH_VARS//|/ }; do
        [[ -n "${!v:-}" ]] && have_auth=1
    done
    grep -qE "^(${AUTH_VARS})=.+" .env 2>/dev/null && have_auth=1
    if [[ $have_auth -ne 1 ]]; then
        echo "bench: harness '$HARNESS' needs ${AUTH_VARS//|/ or } in env or .env" >&2
        exit 2
    fi
fi

export BENCH_RUN_DIR="./state/bench/${RUN_ID}"
export BENCH_RUN_SECONDS="$SECONDS_BUDGET"
export BENCH_HARNESS="$HARNESS"
export BENCH_MODEL="$MODEL"
export BENCH_SEED="$SEED"
# opencode and Cursor drive MCP through the TypeScript SDK, whose tool-call
# timeout defaults to 60s; keep the inline `execute` wait clear of it so a long
# action gets backgrounded (status:"running") instead of failing client-side.
if [[ -z "${BENCH_EXECUTE_WAIT_S:-}" && "$HARNESS" != "claude-code" ]]; then
    export BENCH_EXECUTE_WAIT_S=40
fi
mkdir -p "$BENCH_RUN_DIR"/{video,sessions,harness}
chmod -R a+rwX "$BENCH_RUN_DIR" 2>/dev/null || true

COMPOSE=(docker compose -f docker-compose.yml -f bench/compose.bench.yml)
[[ $LOCAL -eq 1 ]] && COMPOSE=("${COMPOSE[@]}" -f docker-compose.arm64.yml)

log() { echo "[bench $(date +%H:%M:%S)] $*"; }

log "run=$RUN_ID harness=$HARNESS model=$MODEL budget=${SECONDS_BUDGET}s seed=$SEED artifacts=$BENCH_RUN_DIR"

log "building images"
"${COMPOSE[@]}" --profile harness build

log "starting world stack"
"${COMPOSE[@]}" up -d mc-server mc-client mineclaude

log "waiting for the bot to be in-world (bridge /status.health)"
in_world=0
for _ in $(seq 1 180); do  # up to 15 min: server gen + client join is slow
    if curl -sf --max-time 3 localhost:8081/status 2>/dev/null \
        | python3 -c "import json,sys; d=json.load(sys.stdin); d=d.get('data',d); sys.exit(0 if d.get('health') is not None else 1)" 2>/dev/null; then
        in_world=1; break
    fi
    sleep 5
done
if [[ $in_world -ne 1 ]]; then
    log "FAIL: bot never reached the world; dumping logs"
    "${COMPOSE[@]}" logs --tail 100 > "$BENCH_RUN_DIR/logs-failure.txt" || true
    [[ $KEEP -eq 1 ]] || "${COMPOSE[@]}" down -v --remove-orphans
    exit 1
fi

log "waiting for the MCP server (mineclaude :5556)"
mcp_up=0
for _ in $(seq 1 24); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 localhost:5556/mcp 2>/dev/null || echo 000)
    [[ "$code" != "000" ]] && { mcp_up=1; break; }
    sleep 5
done
if [[ $mcp_up -ne 1 ]]; then
    log "FAIL: mineclaude MCP server never answered on :5556 (container crash-looping?)"
    "${COMPOSE[@]}" logs --tail 100 mineclaude > "$BENCH_RUN_DIR/logs-failure.txt" || true
    [[ $KEEP -eq 1 ]] || "${COMPOSE[@]}" down -v --remove-orphans
    exit 1
fi

T0=$(date +%s)
GIT_SHA=$(git rev-parse HEAD 2>/dev/null || echo unknown)
cat > "$BENCH_RUN_DIR/metadata.json" <<EOF
{
  "run_id": "$RUN_ID",
  "harness": "$HARNESS",
  "model": "$MODEL",
  "budget_seconds": $SECONDS_BUDGET,
  "seed": "$SEED",
  "git_sha": "$GIT_SHA",
  "t0_epoch": $T0,
  "started_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

log "starting harness — clock running"
# --no-deps is load-bearing: without it, `up harness` re-evaluates the
# dependency chain and (on some compose versions) RECREATES mc-server —
# wiping the world mid-run and orphaning the client. The world stack is
# already up and gated healthy; the harness must touch nothing but itself.
server_cid=$("${COMPOSE[@]}" ps -q mc-server)
COMPOSE_PROFILES=harness "${COMPOSE[@]}" up -d --no-deps harness
if [[ "$("${COMPOSE[@]}" ps -q mc-server)" != "$server_cid" ]]; then
    log "FATAL: mc-server container was recreated at harness start — world lost, aborting"
    "${COMPOSE[@]}" --profile harness down -v --remove-orphans
    exit 1
fi

hard_stop=$(( T0 + SECONDS_BUDGET + 180 ))  # grace for the last claude invocation to wind down
while :; do
    cid=$(COMPOSE_PROFILES=harness "${COMPOSE[@]}" ps -q harness)
    running=$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null || echo false)
    [[ "$running" != "true" ]] && { log "harness exited"; break; }
    if (( $(date +%s) > hard_stop )); then
        log "harness overran grace period; stopping it"
        docker stop -t 20 "$cid" >/dev/null || true
        break
    fi
    sleep 15
done

log "collecting artifacts"
curl -sf --max-time 10 localhost:8081/advancements > "$BENCH_RUN_DIR/advancements.json" \
    || log "WARN: advancements snapshot failed"
curl -sf --max-time 10 -X POST localhost:8081/record/stop >/dev/null 2>&1 || true
"${COMPOSE[@]}" logs --no-color > "$BENCH_RUN_DIR/logs.txt" 2>&1 || true
docker logs "$(COMPOSE_PROFILES=harness "${COMPOSE[@]}" ps -aq harness)" \
    > "$BENCH_RUN_DIR/harness/container.log" 2>&1 || true

if [[ -s "$BENCH_RUN_DIR/advancements.json" ]]; then
    # Metric is the advancement COUNT; weighted gamerscore is disabled (pass
    # --gamerscore --scoring bench/scoring/gamerscore.json to re-score offline).
    python3 bench/score.py \
        --advancements "$BENCH_RUN_DIR/advancements.json" \
        --sessions "$BENCH_RUN_DIR/sessions" \
        --t0 "$T0" \
        --out "$BENCH_RUN_DIR/score.json"
else
    log "WARN: no advancements snapshot — no score computed"
fi

# Token ledger, folded out of the harness transcripts so cost analysis never has
# to re-download them. Also carries the rate-limit health flag that quarantines a
# throttled trial.
python3 bench/usage.py \
    --harness "$BENCH_RUN_DIR/harness" \
    --kind "$HARNESS" \
    --out "$BENCH_RUN_DIR/usage.json" || log "WARN: usage summary failed"

if [[ $KEEP -eq 1 ]]; then
    log "leaving stack up (--keep)"
else
    log "tearing down"
    "${COMPOSE[@]}" --profile harness down -v --remove-orphans
fi

log "done — artifacts in $BENCH_RUN_DIR"
[[ -f "$BENCH_RUN_DIR/score.json" ]] && python3 -c "
import json
d = json.load(open('$BENCH_RUN_DIR/score.json'))
print(f\"SCORE: {d['earned_count']} advancements\")
for e in d['breakdown']:
    off = f\"+{e['offset_s']:.0f}s\" if e['offset_s'] is not None else '     '
    print(f\"  {off:>8}  {e['title'] or e['id']}\")
"
