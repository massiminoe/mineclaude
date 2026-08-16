#!/usr/bin/env bash
# Claude Code harness loop: set up a workspace, then repeatedly invoke headless
# Claude Code against the run's MCP server until the wall-clock budget expires.
# The container exits on its own at the deadline; the runner treats that exit
# as end-of-run.
set -uo pipefail

MCP_URL="${MCP_URL:-http://mineclaude:5556/mcp}"
MODEL="${BENCH_MODEL:-claude-haiku-4-5-20251001}"
RUN_SECONDS="${BENCH_RUN_SECONDS:-3600}"
ART="${ARTIFACTS_DIR:-/artifacts}"

mkdir -p "$ART"
log() { echo "[harness $(date -u +%H:%M:%S)] $*" | tee -a "$ART/harness.log"; }

# --- wait for the MCP server to answer HTTP (the runner has already gated on
# the bot being in-world; this covers container start ordering). FAIL HARD if
# it never comes up — running the agent without its tools burns real budget
# and tokens on a doomed session (learned the hard way in the first shakeout).
mcp_up=0
for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$MCP_URL" 2>/dev/null || echo 000)
    # Any HTTP response (405/406 to a bare GET is normal for streamable-HTTP)
    # proves the server is listening; 000 means no connection.
    [[ "$code" != "000" ]] && { mcp_up=1; break; }
    sleep 2
done
if [[ $mcp_up -ne 1 ]]; then
    log "FATAL: MCP server at $MCP_URL never answered — refusing to start the agent"
    exit 1
fi

# --- workspace: skill + scoring table + MCP config ---
mkdir -p /workspace/.claude/skills
cp -r /skills/mineclaude /workspace/.claude/skills/mineclaude
# Gamerscore weighting is disabled — the metric is the raw advancement count,
# so the scoring table is deliberately NOT surfaced to the agent (it would
# imply some advancements are worth more than others). The /scoring mount stays
# for when weighted scoring is re-enabled.
cat > /workspace/.mcp.json <<EOF
{"mcpServers": {"mineclaude": {"type": "http", "url": "${MCP_URL}"}}}
EOF

claude --version > "$ART/claude-version.txt" 2>&1 || true
log "model=$MODEL budget=${RUN_SECONDS}s mcp=$MCP_URL claude=$(cat "$ART/claude-version.txt")"

PROMPT="$(cat /opt/bench/prompt.md)

Your time budget for this session is ${RUN_SECONDS} seconds of wall-clock time, starting now."

deadline=$(( $(date +%s) + RUN_SECONDS ))
i=0
while :; do
    left=$(( deadline - $(date +%s) ))
    (( left <= 15 )) && break
    i=$(( i + 1 ))
    if (( i == 1 )); then
        args=( -p "$PROMPT" )
    else
        args=( --continue -p "The session is still live — roughly ${left} seconds remain. Keep playing and earning advancements; do not stop to summarize until time is up." )
    fi
    log "invocation $i starts (${left}s left)"
    timeout --signal=TERM --kill-after=20 "$left" \
        claude "${args[@]}" \
            --model "$MODEL" \
            --dangerously-skip-permissions \
            --mcp-config /workspace/.mcp.json --strict-mcp-config \
            --output-format stream-json --verbose \
            >> "$ART/claude-${i}.jsonl" 2>> "$ART/claude-${i}.err"
    rc=$?
    log "invocation $i exited rc=$rc"
    # Small backoff so a fast-crashing CLI can't spin the loop — and a hard
    # abort if it keeps dying: a systemic failure (bad auth, dead MCP) would
    # otherwise burn the whole budget in a silent retry loop. rc=124 is the
    # timeout kill at the deadline, i.e. normal end-of-budget, not a failure.
    if (( rc != 0 && rc != 124 )); then
        fails=$(( ${fails:-0} + 1 ))
        if (( fails >= 5 )); then
            log "FATAL: $fails consecutive failed invocations — aborting run"
            exit 1
        fi
        sleep 10
    else
        fails=0
    fi
done

log "budget exhausted after $i invocation(s)"
