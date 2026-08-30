#!/usr/bin/env bash
# opencode harness loop: set up a workspace, then repeatedly invoke
# `opencode run` (its non-interactive mode) against the run's MCP server until
# the wall-clock budget expires. The container exits on its own at the deadline;
# the runner treats that exit as end-of-run.
set -uo pipefail
source /opt/bench/common.sh

wait_for_mcp || exit 1

if [[ -z "${OPENCODE_API_KEY:-}" ]]; then
    log "FATAL: OPENCODE_API_KEY is empty — refusing to start the agent"
    exit 1
fi

# --- workspace: skill + MCP config ---
# opencode discovers Claude-format skills at .claude/skills/<name>/SKILL.md, so
# the repo's skill directory mounts in unchanged. They load on demand, hence the
# AGENTS.md pointer.
install_skill "$WORKSPACE/.claude/skills"
write_agents_md ".claude/skills/mineclaude/SKILL.md"

# permission "*": allow alongside --auto — the flag covers the run, the config
# covers anything the flag doesn't, and neither can block on a prompt that no
# human is there to answer.
cat > "$WORKSPACE/opencode.json" <<JSON
{
  "\$schema": "https://opencode.ai/config.json",
  "mcp": {
    "mineclaude": {
      "type": "remote",
      "url": "${MCP_URL}",
      "enabled": true
    }
  },
  "permission": { "*": "allow" }
}
JSON

opencode --version > "$ART/opencode-version.txt" 2>&1 || true
log "model=$BENCH_MODEL budget=${RUN_SECONDS}s mcp=$MCP_URL opencode=$(cat "$ART/opencode-version.txt")"

PROMPT="$(build_prompt)"

start_clock
i=0
while :; do
    left=$(seconds_left)
    (( left <= 15 )) && break
    i=$(( i + 1 ))
    if (( i == 1 )); then
        args=( "$PROMPT" )
    else
        # -c continues the previous session, the counterpart of Claude Code's
        # --continue: one conversation across the budget, not N cold starts.
        args=( -c "$(continue_prompt "$left")" )
    fi
    log "invocation $i starts (${left}s left)"
    timeout --signal=TERM --kill-after=20 "$left" \
        opencode run \
            --model "$BENCH_MODEL" \
            --format json \
            --auto \
            --dir "$WORKSPACE" \
            "${args[@]}" \
            >> "$ART/opencode-${i}.jsonl" 2>> "$ART/opencode-${i}.err"
    rc=$?
    log "invocation $i exited rc=$rc"
    handle_rc "$rc" || exit 1
done

# An independent record of the session, from opencode's own store: `export`
# dumps every message with its token and cost accounting. It is the cross-check
# on the step_finish sums in usage.json — and the artifact that settles whether
# those counts are per-step or cumulative, without a paid re-run to find out.
session_id=$(grep -ho '"sessionID":"[^"]*"' "$ART"/opencode-*.jsonl 2>/dev/null | tail -1 | cut -d'"' -f4)
if [[ -n "$session_id" ]]; then
    if opencode export "$session_id" > "$ART/opencode-session.json" 2>> "$ART/opencode-export.err"; then
        log "exported session $session_id"
    else
        log "WARN: session export failed for $session_id"
    fi
else
    log "WARN: no session id found in transcripts — skipping export"
fi

log "budget exhausted after $i invocation(s)"
