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
    echo "Mods installed in $MODS_DIR:"
    ls -la "$MODS_DIR/"
fi

# Copy Minescript scripts to both possible locations
# (Minescript may use /headlessmc/minescript/ or the game dir)
for SCRIPTS_DIR in "$GAME_DIR/minescript" "/headlessmc/minescript"; do
    mkdir -p "$SCRIPTS_DIR"
    if ls /tmp/scripts/*.py 1>/dev/null 2>&1; then
        cp /tmp/scripts/*.py "$SCRIPTS_DIR/"
    fi
    # Install Minescript config (autorun bridge on world join)
    if [ -f /tmp/scripts/minescript-config.txt ]; then
        cp /tmp/scripts/minescript-config.txt "$SCRIPTS_DIR/config.txt"
    fi
done
echo "Scripts installed"

# Copy bridge package so it's importable from /headlessmc/bridge/
if [ -d /tmp/bridge ]; then
    cp -r /tmp/bridge/ /headlessmc/bridge/
    echo "Bridge package installed at /headlessmc/bridge/"
fi

echo "Launching Fabric 1.21.5 headlessly (offline, in-memory mode)..."

# Start HMC in a screen session so we can send commands via screen -X stuff
screen -dmS hmc bash -c 'hmc launch fabric:1.21.5 -lwjgl -offline -inmemory 2>&1 | tee /tmp/hmc.log'

# Wait for game to load by watching for the blur shader messages
echo "Waiting for game to load..."
for i in $(seq 1 60); do
    sleep 5
    if grep -q "blur/5" /tmp/hmc.log 2>/dev/null; then
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

# Op the bot via RCON
echo "Opping bot via RCON..."
python3 -c "
from mcrcon import MCRcon
try:
    with MCRcon('mc-server', 'mineclaude') as mcr:
        print(mcr.command('op Claude'))
        print(mcr.command('op Massimino'))
except Exception as e:
    print(f'RCON op failed: {e}')
"

# Wait for bridge to be ready (autorun config starts bridge on world join)
echo "Waiting for bridge server..."
for i in $(seq 1 30); do
    if curl -s localhost:8080/status > /dev/null 2>&1; then
        echo "Bridge server ready!"
        break
    fi
    sleep 2
    echo "Waiting for bridge... ($i/30)"
done

# Keep container alive, streaming logs
echo "=== Entering log tail ==="
tail -f /tmp/hmc.log
