#!/bin/bash
set -e

# HeadlessMC runs the game from /headlessmc/HeadlessMC/run
GAME_DIR="/headlessmc/HeadlessMC/run"
mkdir -p "$GAME_DIR"

# Copy mods into the Fabric mods folder (relative to game dir)
MODS_DIR="$GAME_DIR/mods"
mkdir -p "$MODS_DIR"
if ls /tmp/mods/*.jar 1>/dev/null 2>&1; then
    cp /tmp/mods/*.jar "$MODS_DIR/"
fi
# Mineclaude native bridge mod (built in stage 1 of the Dockerfile). Loom
# also writes a -sources.jar alongside the remapped production jar; the
# extglob filter below keeps only the latter so Fabric doesn't try to load
# the sources artifact as a mod.
if [ -d /tmp/mod-builder-libs ]; then
    # Loom emits a remapped production jar plus -sources/-dev variants; only
    # the production jar is loadable as a mod, so skip the others by suffix.
    for jar in /tmp/mod-builder-libs/*.jar; do
        case "$jar" in
            *-sources.jar|*-dev.jar) continue ;;
        esac
        [ -e "$jar" ] && cp "$jar" "$MODS_DIR/"
    done
fi
echo "Mods installed in $MODS_DIR:"
ls -la "$MODS_DIR/"

# Copy MC render options into game dir
if [ -f /tmp/options.txt ]; then
    cp /tmp/options.txt "$GAME_DIR/options.txt"
    echo "MC options.txt installed"
fi

# Start virtual framebuffer for real rendering
echo "Starting Xvfb virtual display..."
Xvfb :99 -screen 0 854x480x24 -ac +extension GLX +render -noreset &
export DISPLAY=:99
export LIBGL_ALWAYS_SOFTWARE=1
sleep 1
echo "Xvfb started on :99"

echo "Launching Fabric 1.21.5 with rendering (offline, in-memory mode)..."

# Start HMC in a screen session
# NOTE: base image sets hmc.invert.lwjgl.flag=true, so -lwjgl DISABLES the stub (real rendering)
screen -dmS hmc bash -c 'DISPLAY=:99 LIBGL_ALWAYS_SOFTWARE=1 hmc launch fabric:1.21.5 -lwjgl -offline -inmemory 2>&1 | tee /tmp/hmc.log'

# Wait for game to load
# With -lwjgl (headless), marker is "blur/5"; with Xvfb (real rendering), marker is "gui.png-atlas"
echo "Waiting for game to load..."
for i in $(seq 1 60); do
    sleep 5
    if grep -q "blur/5\|gui.png-atlas" /tmp/hmc.log 2>/dev/null; then
        echo "Game loaded! (detected at iteration $i)"
        break
    fi
    echo "Still waiting... ($i/60)"
done

# Connect to the server
echo "Connecting to mc-server..."
screen -S hmc -p 0 -X stuff "connect mc-server\n"
sleep 15

echo "=== Connection status ==="
grep -i "world\|connect\|join\|baritone.*data" /tmp/hmc.log | tail -10

# Op the bot and set game rules via RCON
echo "Opping bot and configuring game rules via RCON..."
python3 -c "
from mcrcon import MCRcon
try:
    with MCRcon('mc-server', 'mineclaude') as mcr:
        print(mcr.command('op Claude'))
        print(mcr.command('op Massimino'))
        print(mcr.command('gamerule doImmediateRespawn true'))
except Exception as e:
    print(f'RCON failed: {e}')
"

# Wait for the native Fabric mod bridge to be ready. The mod boots its
# JDK HttpServer once Minecraft finishes initializing the client; we
# poll /health (not /status — /status needs an active world) on 8081.
echo "Waiting for bridge server..."
for i in $(seq 1 30); do
    if curl -s localhost:8081/health > /dev/null 2>&1; then
        echo "Bridge server ready!"
        break
    fi
    sleep 2
    echo "Waiting for bridge... ($i/30)"
done

# Keep container alive, streaming logs
echo "=== Entering log tail ==="
tail -f /tmp/hmc.log
