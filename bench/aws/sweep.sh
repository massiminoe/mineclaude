#!/usr/bin/env bash
# Run N bench trials of the SAME (harness, model, seed) on ephemeral EC2 VMs
# and aggregate the scores — the variance driver for a benchmark entry.
#
#   bench/aws/sweep.sh [--trials 2] [--concurrency 2] [--harness <name>]
#                      [--model <id>] [--seconds 3600] [--seed <s>]
#                      [--git-ref <sha|branch>] [--type c7i.2xlarge] [--spot]
#                      [--sweep-id <id>]
#
# Each trial is a fully independent VM (own world, own bot, own harness), so
# --concurrency is bounded by two things OUTSIDE AWS as much as inside it:
#   1. Provider rate limits — every trial authenticates with the SAME
#      subscription credential from SSM. A throttled harness scores low for
#      reasons that have nothing to do with the model, which silently corrupts
#      the result. Keep concurrency low on a subscription. This bites hardest on
#      the dollar-metered plans: opencode Go caps at $12 per 5h and $30 a week,
#      and Cursor Pro's monthly pool is $20 — a wave of concurrent trials can
#      exhaust either mid-sweep.
#   2. EC2 vCPU quota — c7i.2xlarge is 8 vCPU each; the common default
#      on-demand standard quota (32) caps you at 4 concurrent.
# Trials run in waves of --concurrency: a wave is launched, waited out, then
# the next is launched.
set -euo pipefail
cd "$(dirname "$0")/../.."

REGION="${AWS_REGION:-us-east-1}"
TRIALS=2
CONCURRENCY=2
HARNESS="claude-code"
MODEL="claude-sonnet-5"
SECONDS_BUDGET=3600
SEED="mineclaude-bench-1"
GIT_REF="$(git rev-parse HEAD)"
ITYPE="c7i.2xlarge"
SPOT=0
SWEEP_ID="$(date +%Y%m%d-%H%M%S)"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --trials)      TRIALS="$2"; shift 2 ;;
        --concurrency) CONCURRENCY="$2"; shift 2 ;;
        --harness)     HARNESS="$2"; shift 2 ;;
        --model)       MODEL="$2"; shift 2 ;;
        --seconds)     SECONDS_BUDGET="$2"; shift 2 ;;
        --seed)        SEED="$2"; shift 2 ;;
        --git-ref)     GIT_REF="$2"; shift 2 ;;
        --type)        ITYPE="$2"; shift 2 ;;
        --sweep-id)    SWEEP_ID="$2"; shift 2 ;;
        --spot)        SPOT=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="mineclaude-bench-${ACCOUNT}"
DEST="state/bench/sweep-${SWEEP_ID}"
mkdir -p "$DEST"

# Same budget the VM's deadman uses (run + provisioning slack), plus polling slack.
MAX_MINUTES=$(( SECONDS_BUDGET / 60 + 55 ))

log() { echo "[sweep $(date +%H:%M:%S)] $*"; }

log "sweep=$SWEEP_ID trials=$TRIALS concurrency=$CONCURRENCY harness=$HARNESS model=$MODEL budget=${SECONDS_BUDGET}s seed=$SEED ref=${GIT_REF:0:12}"
log "artifacts -> $DEST | s3://$BUCKET/runs/"

RUN_IDS=()
launch_one() {  # $1 = trial index
    local n="$1" run_id out iid
    run_id="${SWEEP_ID}-t${n}"
    local args=(--seconds "$SECONDS_BUDGET" --harness "$HARNESS" --model "$MODEL"
                --seed "$SEED" --run-id "$run_id" --type "$ITYPE" --git-ref "$GIT_REF"
                --no-wait)
    [[ $SPOT -eq 1 ]] && args+=(--spot)
    out=$(bench/aws/launch.sh "${args[@]}" 2>&1) || { log "trial $n FAILED to launch:"; echo "$out" >&2; return 1; }
    iid=$(sed -n 's/^launched \(i-[a-z0-9]*\).*/\1/p' <<<"$out" | head -1)
    echo "$run_id $iid" >> "$DEST/instances.txt"
    log "trial $n launched: run=$run_id instance=${iid:-unknown}"
    RUN_IDS+=("$run_id")
}

# Wait for every run id in a wave to drop a score.json in S3 (or for its
# instance to die without one).
wait_wave() {
    local -a pending=("$@")
    local deadline=$(( $(date +%s) + MAX_MINUTES * 60 ))
    while (( ${#pending[@]} > 0 )); do
        sleep 60
        local -a still=()
        for run_id in "${pending[@]}"; do
            if aws s3 ls "s3://$BUCKET/runs/$run_id/score.json" --region "$REGION" >/dev/null 2>&1; then
                mkdir -p "$DEST/$run_id"
                aws s3 cp --only-show-errors "s3://$BUCKET/runs/$run_id/score.json" "$DEST/$run_id/" --region "$REGION"
                aws s3 cp --only-show-errors "s3://$BUCKET/runs/$run_id/metadata.json" "$DEST/$run_id/" --region "$REGION" 2>/dev/null || true
                aws s3 cp --only-show-errors "s3://$BUCKET/runs/$run_id/usage.json" "$DEST/$run_id/" --region "$REGION" 2>/dev/null || true
                local n note
                n=$(python3 -c "import json;print(json.load(open('$DEST/$run_id/score.json'))['earned_count'])" 2>/dev/null || echo '?')
                note=$(python3 -c "
import json
u = json.load(open('$DEST/$run_id/usage.json'))
c = u.get('cost_usd')
print((f\", \${c:.2f}\" if c is not None else ', cost n/a') + (' THROTTLED' if u['health']['throttled'] else ''))
" 2>/dev/null || echo '')
                log "  $run_id DONE — $n advancements$note"
                continue
            fi
            local iid state
            iid=$(awk -v r="$run_id" '$1==r{print $2}' "$DEST/instances.txt" 2>/dev/null | head -1)
            state=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$iid" \
                --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo unknown)
            if [[ "$state" == "terminated" ]]; then
                log "  $run_id TERMINATED without a score — see s3://$BUCKET/runs/$run_id/bench-userdata.log"
                continue
            fi
            still+=("$run_id")
        done
        pending=("${still[@]+"${still[@]}"}")
        (( ${#pending[@]} > 0 )) && log "  waiting on ${#pending[@]}: ${pending[*]}"
        if (( $(date +%s) > deadline )); then
            log "  wave timed out; still pending: ${pending[*]:-none}"
            break
        fi
    done
}

trial=1
while (( trial <= TRIALS )); do
    wave=()
    for (( k = 0; k < CONCURRENCY && trial <= TRIALS; k++, trial++ )); do
        launch_one "$trial" && wave+=("${SWEEP_ID}-t${trial}")
    done
    (( ${#wave[@]} == 0 )) && { log "no trials launched in this wave — aborting"; exit 1; }
    log "wave of ${#wave[@]} running; polling every 60s (up to ${MAX_MINUTES}m)"
    wait_wave "${wave[@]}"
done

log "sweep complete — aggregating"
python3 - "$DEST" "$MODEL" "$SEED" "$SECONDS_BUDGET" "$HARNESS" <<'PY'
import json, sys, statistics
from collections import Counter
from pathlib import Path

dest, model, seed, budget, harness = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
runs = sorted(p for p in dest.glob("*/score.json"))
if not runs:
    print("no score.json files collected"); sys.exit(1)

counts, cost, throttled, freq = {}, {}, set(), Counter()
for p in runs:
    d = json.loads(p.read_text())
    run_id = p.parent.name
    counts[run_id] = d["earned_count"]
    usage_path = p.parent / "usage.json"
    if usage_path.exists():
        u = json.loads(usage_path.read_text())
        # None when the harness could not report cost (e.g. Cursor's billed
        # figure had not landed yet) — absent, not zero.
        if u.get("cost_usd") is not None:
            cost[run_id] = u["cost_usd"]
        # A throttled trial measured the rate limiter, not the model. Reported,
        # but held out of the mean and the hit-rate rather than silently averaged.
        if u["health"]["throttled"]:
            throttled.add(run_id)
    if run_id not in throttled:
        for e in d["breakdown"]:
            freq[e["title"] or e["id"]] += 1

valid = [r for r in counts if r not in throttled]
n = len(valid)
print(f"\n=== sweep: {harness} + {model} | seed={seed} | budget={budget}s | {n} valid trial(s) ===")
for run_id, c in counts.items():
    tags = f"  ${cost[run_id]:6.2f}" if run_id in cost else ""
    tags += "  THROTTLED — held out" if run_id in throttled else ""
    print(f"  {run_id:<28} {c:>3} advancements{tags}")
if not n:
    print("  every trial was throttled — no valid mean"); sys.exit(1)
vals = [counts[r] for r in valid]
spread = f"  min {min(vals)}  max {max(vals)}"
sd = f"  sd {statistics.stdev(vals):.1f}" if n > 1 else ""
print(f"  {'MEAN':<28} {statistics.mean(vals):>5.1f}{spread}{sd}")
spend = [cost[r] for r in valid if r in cost]
if spend:
    per_adv = sum(spend) / sum(vals) if sum(vals) else 0
    print(f"  {'COST (list-price est.)':<28} ${statistics.mean(spend):>5.2f}/run   ${per_adv:.2f}/advancement")
print(f"\n  earned in how many of {n} trial(s):")
for title, c in freq.most_common():
    print(f"    {c}/{n}  {title}")
PY

echo
echo "full artifacts: aws s3 cp --recursive s3://$BUCKET/runs/<run-id>/ $DEST/<run-id>/"
