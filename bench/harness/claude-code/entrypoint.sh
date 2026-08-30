#!/usr/bin/env bash
# Claude Code harness loop: set up a workspace, then repeatedly invoke headless
# Claude Code against the run's MCP server until the wall-clock budget expires.
# The container exits on its own at the deadline; the runner treats that exit
# as end-of-run.
set -uo pipefail
source /opt/bench/common.sh

wait_for_mcp || exit 1

# --- workspace: skill + MCP config ---
install_skill "$WORKSPACE/.claude/skills"
# Gamerscore weighting is disabled — the metric is the raw advancement count,
# so the scoring table is deliberately NOT surfaced to the agent (it would
# imply some advancements are worth more than others). The /scoring mount stays
# for when weighted scoring is re-enabled.
cat > "$WORKSPACE/.mcp.json" <<JSON
{"mcpServers": {"mineclaude": {"type": "http", "url": "${MCP_URL}"}}}
JSON

claude --version > "$ART/claude-version.txt" 2>&1 || true
log "model=$BENCH_MODEL budget=${RUN_SECONDS}s mcp=$MCP_URL claude=$(cat "$ART/claude-version.txt")"

PROMPT="$(build_prompt)"

start_clock
i=0
while :; do
    left=$(seconds_left)
    (( left <= 15 )) && break
    i=$(( i + 1 ))
    if (( i == 1 )); then
        args=( -p "$PROMPT" )
    else
        args=( --continue -p "$(continue_prompt "$left")" )
    fi
    log "invocation $i starts (${left}s left)"
    timeout --signal=TERM --kill-after=20 "$left" \
        claude "${args[@]}" \
            --model "$BENCH_MODEL" \
            --dangerously-skip-permissions \
            --mcp-config "$WORKSPACE/.mcp.json" --strict-mcp-config \
            --output-format stream-json --verbose \
            >> "$ART/claude-${i}.jsonl" 2>> "$ART/claude-${i}.err"
    rc=$?
    log "invocation $i exited rc=$rc"
    handle_rc "$rc" || exit 1
done

log "budget exhausted after $i invocation(s)"
