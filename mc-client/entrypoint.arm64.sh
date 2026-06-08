#!/bin/bash
# arm64-native entrypoint for the headless MC client.
#
# Why this differs from entrypoint.sh (the amd64 path): on arm64 the amd64-only
# HeadlessMC base image is gone — we run a native arm64 JVM with arm64 LWJGL
# natives (no Rosetta/qemu emulation, ~4x less RAM). But two arm64-specific
# facts force a different launch dance than the amd64 `screen + connect` flow:
#
#   1. HMC must relaunch the JVM once to set -Djava.library.path for natives,
#      which splits it into wrapper + game JVMs. HMC's in-memory `connect`
#      runtime command reads the *wrapper's* stdin, but the game JVM is a
#      background process group on the wrapper's tty -> any stdin we inject
#      (screen `stuff`) hits SIGTTIN and is dropped. So `connect mc-server`
#      (which works in the amd64 single-JVM path) silently no-ops here.
#   2. HMC's launch parser drops MC's native `--quickPlayMultiplayer` game arg,
#      so we can't ask HMC to auto-join either.
#
# Solution (validated end-to-end): let HMC do what only it can — download the
# version + libraries + assets and build the exact game launch command — then
# BYPASS it. We snapshot the game JVM's argv from /proc, kill HMC, and re-run
# that command ourselves with `--quickPlayMultiplayer` appended. MC's own
# quickPlay joins the server with no console needed. The bridge mod loads the
# same way (it's in the Fabric mods dir).
set -e

GAME_DIR="/headlessmc/HeadlessMC/run"
MODS_DIR="$GAME_DIR/mods"
HMC_JAR="/headlessmc/headlessmc-launcher-wrapper.jar"
ARM64_NATIVES="/opt/arm64-natives"
MC_SERVER="${MC_SERVER:-mc-server}"
BOOT_LOG="/tmp/hmc-bootstrap.log"
GAME_LOG="/tmp/hmc.log"   # the real run logs here (mirrors amd64 path for tooling)

mkdir -p "$MODS_DIR"

# --- Bot name (override for running several clients on one server) ----------
# Each parallel agent needs a distinct player name. BOT_NAME rewrites HMC's
# offline username so the captured launch command (and thus the joined player)
# uses it. Defaults to Claude to match the amd64 path.
BOT_NAME="${BOT_NAME:-Claude}"
CONFIG="/headlessmc/HeadlessMC/config.properties"
if grep -q '^hmc.offline.username=' "$CONFIG" 2>/dev/null; then
    sed -i "s/^hmc.offline.username=.*/hmc.offline.username=$BOT_NAME/" "$CONFIG"
else
    echo "hmc.offline.username=$BOT_NAME" >> "$CONFIG"
fi
echo "Bot name: $BOT_NAME"

# --- Install mods (identical to amd64 entrypoint) --------------------------
if ls /tmp/mods/*.jar 1>/dev/null 2>&1; then
    cp /tmp/mods/*.jar "$MODS_DIR/"
fi
if [ -d /tmp/mod-builder-libs ]; then
    for jar in /tmp/mod-builder-libs/*.jar; do
        case "$jar" in
            *-sources.jar|*-dev.jar) continue ;;
        esac
        [ -e "$jar" ] && cp "$jar" "$MODS_DIR/"
    done
fi
echo "Mods installed in $MODS_DIR:"
ls -la "$MODS_DIR/"

[ -f /tmp/options.txt ] && cp /tmp/options.txt "$GAME_DIR/options.txt" && echo "MC options.txt installed"

# --- Virtual framebuffer (kept up across both launches) --------------------
echo "Clearing any stale X locks..."
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
echo "Starting Xvfb virtual display..."
Xvfb :99 -screen 0 854x480x24 -ac +extension GLX +render -noreset &
export DISPLAY=:99
export LIBGL_ALWAYS_SOFTWARE=1
sleep 1
echo "Xvfb started on :99"

# --- Phase 1: bootstrap via HMC -------------------------------------------
# Renders to the title screen (the arm64 LWJGL natives are merged into the
# natives-linux jars at build, so HMC extracts aarch64 .so straight from them —
# no librarypath needed) and, crucially, forks the game as its own process whose
# /proc/<pid>/cmdline is the exact launch command we replay in phase 2.
#
# cd /headlessmc so HMC reads our config (gamedir=/headlessmc/HeadlessMC/run,
# offline.username) — otherwise it defaults to --gameDir /root/.minecraft and
# ignores our mods dir + options.txt (=> no bridge, onboarding modal blocks
# quickPlay). Deliberately NOT -inmemory: in-memory runs the game inside the
# launcher JVM, leaving no KnotClient child process to snapshot.
cd /headlessmc
echo "Bootstrap: HMC building the game launch command (libraries + assets are baked)..."
rm -f "$BOOT_LOG"
java -jar "$HMC_JAR" --command launch fabric:1.21.5 -lwjgl -offline > "$BOOT_LOG" 2>&1 &

# Wait for the real-rendering marker (gui.png-atlas). Generous: first run
# downloads ~300 MB of assets.
echo "Waiting for bootstrap render (first run downloads assets, can take minutes)..."
for i in $(seq 1 120); do
    grep -q "gui.png-atlas" "$BOOT_LOG" 2>/dev/null && { echo "Bootstrap rendered (iter $i)."; break; }
    sleep 3
done

# --- Snapshot the game JVM's launch command --------------------------------
# The in-memory game runs under Fabric's KnotClient; find that JVM and dump its
# argv. (cmdline is NUL-separated; paths contain no spaces so a space-joined
# form re-runs cleanly.)
echo "Capturing game launch command..."
GAME_PID=""
for attempt in $(seq 1 10); do
    for p in $(pgrep java); do
        if tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "KnotClient"; then
            GAME_PID="$p"; break
        fi
    done
    [ -n "$GAME_PID" ] && break
    sleep 2
done
if [ -z "$GAME_PID" ]; then
    echo "FATAL: could not find the game JVM to snapshot. Bootstrap log tail:"
    tail -30 "$BOOT_LOG"
    exit 1
fi
# Snapshot argv preserving EMPTY fields. An offline launch carries empty
# --accessToken/--clientId/--xuid; space-joining then unquoted re-expansion
# would collapse those, shifting every following game arg and mangling auth +
# quickPlay (symptom: "Failed to parse into SignedJWT: --clientId", stuck at a
# black title screen). mapfile -d '' reads the NUL-separated cmdline verbatim.
mapfile -d '' GAME_ARGS < "/proc/$GAME_PID/cmdline"
# /proc cmdline has a terminating NUL which can yield a trailing empty element;
# drop it (a genuine empty arg is always mid-array here, never last).
[ -n "${GAME_ARGS[-1]:-}" ] || unset 'GAME_ARGS[-1]'
echo "Captured game command from pid $GAME_PID (${#GAME_ARGS[@]} args)."

# Natives need no handling here: the bootstrap above already extracted the
# aarch64 LWJGL .so (from the build-merged natives jars) into the per-launch dir
# that the captured argv's -Djava.library.path / SharedLibraryExtractPath point
# at, and SIGKILLing the bootstrap leaves that dir intact for the relaunch.

# Force the player name as a safety net (config should already supply it via
# cd-read hmc.offline.username). Rewrite the value after --username.
for i in "${!GAME_ARGS[@]}"; do
    if [ "${GAME_ARGS[$i]}" = "--username" ]; then
        GAME_ARGS[$((i + 1))]="$BOT_NAME"; break
    fi
done

# --- Kill the bootstrap (SIGKILL => no cleanup, extracted dir persists) -----
echo "Stopping bootstrap HMC..."
pkill -9 java 2>/dev/null || true
sleep 2

# --- Phase 2: direct relaunch with native quickPlay auto-join --------------
echo "Launching game directly and auto-joining $MC_SERVER via quickPlay (as $BOT_NAME)..."
rm -f "$GAME_LOG"
cd "$GAME_DIR"
setsid "${GAME_ARGS[@]}" --quickPlayMultiplayer "$MC_SERVER" > "$GAME_LOG" 2>&1 < /dev/null &

echo "Waiting for server connection..."
for i in $(seq 1 40); do
    grep -qiE "Connecting to|joined the game|Loaded .* advancements" "$GAME_LOG" 2>/dev/null && { echo "Connecting/joined (iter $i)."; break; }
    sleep 3
done
echo "=== Connection status ==="
grep -iE "Connecting to|joined|advancement" "$GAME_LOG" 2>/dev/null | grep -ivE "Realms|authoriz" | tail -5 || true

# --- Game rules via RCON (opping is handled server-side) -------------------
echo "Configuring game rules via RCON..."
python3 -c "
from mcrcon import MCRcon
try:
    with MCRcon('$MC_SERVER', 'mineclaude') as mcr:
        print(mcr.command('gamerule doImmediateRespawn true'))
        print(mcr.command('gamerule keepInventory true'))
except Exception as e:
    print(f'RCON failed: {e}')
"

# --- Wait for the native bridge HTTP server --------------------------------
echo "Waiting for bridge server..."
for i in $(seq 1 30); do
    if curl -s localhost:8081/health > /dev/null 2>&1; then
        echo "Bridge server ready!"
        break
    fi
    sleep 2
    echo "Waiting for bridge... ($i/30)"
done

echo "=== Entering log tail ==="
tail -f "$GAME_LOG"
