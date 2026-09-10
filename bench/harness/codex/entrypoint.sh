#!/usr/bin/env bash
set -uo pipefail
source /opt/bench/common.sh
# The writable auth mount is separate from all uploaded artifacts. Codex must
# retain refreshed subscription tokens; never fall back to API billing.
export CODEX_HOME=/codex-auth
unset OPENAI_API_KEY CODEX_API_KEY
# Timeout kills the CLI before turn.completed; preserve its latest cumulative
# session usage on every exit, including failed invocations.
trap 'node /opt/bench/collect_usage.mjs || log "WARN: session usage collection failed"' EXIT
if ! jq -e '.auth_mode == "chatgpt" and (.tokens.refresh_token | type == "string" and length > 0) and (.OPENAI_API_KEY | not)' "$CODEX_HOME/auth.json" >/dev/null 2>&1; then
    log 'FATAL: a ChatGPT subscription auth.json is required'
    exit 1
fi
cat > "$CODEX_HOME/config.toml" <<EOF_CONFIG
cli_auth_credentials_store = "file"
forced_login_method = "chatgpt"
model = "$BENCH_MODEL"
approval_policy = "never"
sandbox_mode = "danger-full-access"
[mcp_servers.mineclaude]
url = "$MCP_URL"
required = true
default_tools_approval_mode = "approve"
tool_timeout_sec = 90
EOF_CONFIG
chmod 600 "$CODEX_HOME/config.toml"
wait_for_mcp || exit 1
install_skill "$WORKSPACE/.agents/skills"
write_agents_md '.agents/skills/mineclaude/SKILL.md'
cd "$WORKSPACE" || exit 1
codex --version > "$ART/codex-version.txt" 2>&1
log "model=$BENCH_MODEL budget=${RUN_SECONDS}s codex=$(cat "$ART/codex-version.txt")"
PROMPT="$(build_prompt)"
start_clock
i=0
session_id=''
while :; do
    left=$(seconds_left)
    (( left <= 15 )) && break
    i=$(( i + 1 ))
    args=(exec)
    if [[ -n "$session_id" ]]; then
        args+=(resume "$session_id")
        prompt="$(continue_prompt "$left")"
    else
        prompt="$PROMPT"
    fi
    log "invocation $i starts (${left}s left)"
    timeout --signal=TERM --kill-after=20 "$left" \
        codex "${args[@]}" --json --skip-git-repo-check --model "$BENCH_MODEL" "$prompt" \
        > "$ART/codex-${i}.jsonl" 2> "$ART/codex-${i}.err"
    rc=$?
    # Resume only the explicit session emitted by this run, including after a
    # failed turn; never accidentally resume an unrelated cached session.
    new_session=$(jq -r 'select(.type == "thread.started") | .thread_id' "$ART/codex-${i}.jsonl" 2>/dev/null | head -1)
    [[ -n "$new_session" ]] && session_id="$new_session"
    log "invocation $i exited rc=$rc"
    handle_rc "$rc" || exit 1
done
log "budget exhausted after $i invocation(s)"
