#!/usr/bin/env bash
# Shared harness contract — sourced by every bench/harness/*/entrypoint.sh.
#
# A harness image differs from its siblings only in WHICH CLI it drives. Anything
# that must behave identically across harnesses for scores to stay comparable
# lives here: the artifacts dir, the MCP readiness gate, the prompt, and the
# failure policy. Adding a harness should mean writing a loop body, not
# re-deriving the contract.
#
# Env contract (set by bench/compose.bench.yml):
#   MCP_URL            the mineclaude MCP endpoint to drive
#   BENCH_MODEL        model id for this entry in the eval matrix
#   BENCH_RUN_SECONDS  wall-clock budget; the container exits by itself
#   ARTIFACTS_DIR      where transcripts/logs get written (default /artifacts)
# Mounts: /skills (ro), /artifacts (rw).

MCP_URL="${MCP_URL:-http://mineclaude:5556/mcp}"
# No default model: a wrong-but-plausible default would silently produce a scored
# run for the wrong benchmark entry. The runner always sets it.
BENCH_MODEL="${BENCH_MODEL:?BENCH_MODEL must be set}"
RUN_SECONDS="${BENCH_RUN_SECONDS:-3600}"
ART="${ARTIFACTS_DIR:-/artifacts}"
WORKSPACE="${WORKSPACE:-/workspace}"

mkdir -p "$ART"

log() { echo "[harness $(date -u +%H:%M:%S)] $*" | tee -a "$ART/harness.log"; }

# --- readiness -------------------------------------------------------------
# Gate the agent on the MCP server answering HTTP (the runner has already gated
# on the bot being in-world; this covers container start ordering). FAIL HARD if
# it never comes up — running the agent without its tools burns real budget and
# tokens on a doomed session (learned the hard way in the first shakeout).
wait_for_mcp() {
    local code
    for _ in $(seq 1 30); do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$MCP_URL" 2>/dev/null || echo 000)
        # Any HTTP response (405/406 to a bare GET is normal for streamable-HTTP)
        # proves the server is listening; 000 means no connection.
        [[ "$code" != "000" ]] && return 0
        sleep 2
    done
    log "FATAL: MCP server at $MCP_URL never answered — refusing to start the agent"
    return 1
}

# --- workspace -------------------------------------------------------------
# Install the mineclaude skill into a harness's own discovery path. The skill
# itself is identical everywhere; only the directory each CLI looks in differs
# (.claude/skills for Claude Code + opencode, .cursor/skills for Cursor).
install_skill() {  # $1 = skills dir, e.g. /workspace/.claude/skills
    mkdir -p "$1"
    cp -r /skills/mineclaude "$1/mineclaude"
}

# Skills load on demand in opencode and Cursor — the agent has to choose to open
# one. A pointer file in the workspace root makes that deterministic instead of
# leaving it to description matching. Claude Code lists skills up front and
# doesn't need this.
write_agents_md() {  # $1 = skill path relative to the workspace root
    cat > "$WORKSPACE/AGENTS.md" <<AGENTS
# Mineclaude

You drive a headless Minecraft bot over the \`mineclaude\` MCP server.

**Read \`$1\` before your first action.** It documents the MCP tools, the
primitive vocabulary you run inside \`execute\`, the event and reflex model, and
proven patterns for mining, crafting, building, and combat. The files it links
(\`mental-model.md\`, \`primitives.md\`, \`snippets.md\`, \`handlers.md\`,
\`events.md\`, \`tools.md\`) sit beside it — open them as you need them.
AGENTS
}

# --- prompt ----------------------------------------------------------------
# The task, shared verbatim across harnesses, plus the budget line.
build_prompt() {
    printf '%s\n\nYour time budget for this session is %s seconds of wall-clock time, starting now.\n' \
        "$(cat /opt/bench/prompt.md)" "$RUN_SECONDS"
}

# What every continuation invocation says. Kept identical across harnesses so a
# model is never advantaged by a differently-worded nudge.
continue_prompt() {  # $1 = seconds left
    printf 'The session is still live — roughly %s seconds remain. Keep playing and earning advancements; do not stop to summarize until time is up.' "$1"
}

# --- budget loop -----------------------------------------------------------
DEADLINE=0
start_clock() { DEADLINE=$(( $(date +%s) + RUN_SECONDS )); }
seconds_left() { echo $(( DEADLINE - $(date +%s) )); }

# Failure policy: a small backoff so a fast-crashing CLI can't spin the loop, and
# a hard abort if it keeps dying — a systemic failure (bad auth, dead MCP) would
# otherwise burn the whole budget in a silent retry loop. rc=124 is the timeout
# kill at the deadline, i.e. normal end-of-budget, not a failure.
FAILS=0
handle_rc() {  # $1 = rc; returns 1 when the run should abort
    if (( $1 != 0 && $1 != 124 )); then
        FAILS=$(( FAILS + 1 ))
        if (( FAILS >= 5 )); then
            log "FATAL: $FAILS consecutive failed invocations — aborting run"
            return 1
        fi
        sleep 10
    else
        FAILS=0
    fi
    return 0
}
