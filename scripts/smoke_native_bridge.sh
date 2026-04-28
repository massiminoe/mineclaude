#!/usr/bin/env bash
# Smoke test for the native Fabric mod bridge.
#
# Assumes `docker compose up -d` is already running. Verifies:
#   1. The mineclaude-bridge mod loaded inside the MC client.
#   2. The native HTTP server is listening on :8081 and /health responds
#      with kind=native-mod (so we know we're not accidentally hitting
#      something else bound to that port).
set -euo pipefail

cd "$(dirname "$0")/.."

BRIDGE_URL="${BRIDGE_URL:-http://localhost:8081}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

echo "waiting for bridge at $BRIDGE_URL/health ..."
for i in $(seq 1 45); do
    if curl -fsS "$BRIDGE_URL/health" > /dev/null 2>&1; then
        green "bridge up after ${i}x2s"
        break
    fi
    sleep 2
    [ "$i" = 45 ] && { red "bridge never came up"; exit 1; }
done

echo
echo "--- /health ---"
curl -fsS "$BRIDGE_URL/health" | python3 -m json.tool

# Identity check — guards against something else bound to 8081.
KIND=$(curl -fsS "$BRIDGE_URL/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"].get("kind",""))')
if [ "$KIND" != "native-mod" ]; then
    red "expected data.kind=native-mod on $BRIDGE_URL/health, got '$KIND'"
    exit 1
fi
green "bridge identifies as native-mod"

# Look for the mod's startup line in the MC client logs.
echo
echo "--- mod startup line ---"
if docker compose logs mc-client 2>&1 | grep -F "mineclaude bridge: starting"; then
    green "mod startup line found"
else
    red "mod startup line not found in mc-client logs — mod did not initialize"
    exit 1
fi

green "smoke test passed"
