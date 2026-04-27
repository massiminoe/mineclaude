#!/usr/bin/env bash
# Phase 0 smoke test for the native Fabric mod bridge.
#
# Assumes `docker compose up -d` is already running. Verifies:
#   1. The mineclaude_bridge mod loaded inside the MC client.
#   2. The native HTTP server is listening on :8081 and /health responds.
#   3. The legacy Minescript bridge on :8080 still works.
set -euo pipefail

cd "$(dirname "$0")/.."

NATIVE_URL="${BRIDGE_URL_NATIVE:-http://localhost:8081}"
LEGACY_URL="${BRIDGE_URL:-http://localhost:8080}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

# Wait up to 90s for MC + both bridges to be reachable.
echo "waiting for native bridge at $NATIVE_URL/health ..."
for i in $(seq 1 45); do
    if curl -fsS "$NATIVE_URL/health" > /dev/null 2>&1; then
        green "native bridge up after ${i}x2s"
        break
    fi
    sleep 2
    [ "$i" = 45 ] && { red "native bridge never came up"; exit 1; }
done

echo
echo "--- /health on native (8081) ---"
curl -fsS "$NATIVE_URL/health" | python3 -m json.tool

echo
echo "--- /health on legacy (8080) ---"
curl -fsS "$LEGACY_URL/health" | python3 -m json.tool

# Sanity check: the native /health body must report kind=native-mod and the
# `ported` list. If the legacy bridge is accidentally bound to 8081 we'd see
# the rpc_timeouts_total field instead.
NATIVE_KIND=$(curl -fsS "$NATIVE_URL/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"].get("kind",""))')
if [ "$NATIVE_KIND" != "native-mod" ]; then
    red "expected data.kind=native-mod on $NATIVE_URL/health, got '$NATIVE_KIND'"
    exit 1
fi
green "native bridge identifies as native-mod"

# Look for our mod's startup line in the MC client logs.
echo
echo "--- mod startup line ---"
if docker compose exec -T mc-client grep -F "mineclaude bridge: starting" /tmp/hmc.log; then
    green "mod startup line found"
else
    red "mod startup line not found in /tmp/hmc.log — mod did not initialize"
    exit 1
fi

green "Phase 0 smoke test passed"
