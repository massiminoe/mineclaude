#!/usr/bin/env bash
# Cursor harness: prepare the workspace, then hand the whole budget to
# driver.mjs, which owns the invocation loop (see the note there on why this
# harness drives @cursor/sdk instead of the cursor-agent CLI).
set -uo pipefail
source /opt/bench/common.sh

wait_for_mcp || exit 1

if [[ -z "${CURSOR_API_KEY:-}" ]]; then
    log "FATAL: CURSOR_API_KEY is empty — refusing to start the agent"
    exit 1
fi

# Cursor's own skill discovery path, plus the workspace pointer (skills load on
# demand here, unlike Claude Code which lists them up front).
install_skill "$WORKSPACE/.cursor/skills"
write_agents_md ".cursor/skills/mineclaude/SKILL.md"

node --version > "$ART/cursor-version.txt" 2>&1
node -e 'console.log("@cursor/sdk " + JSON.parse(require("fs").readFileSync("/opt/bench/node_modules/@cursor/sdk/package.json")).version)' \
    >> "$ART/cursor-version.txt" 2>&1 || true
log "harness=cursor $(tr '\n' ' ' < "$ART/cursor-version.txt")"

exec node /opt/bench/driver.mjs
