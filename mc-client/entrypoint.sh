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

# Start virtual framebuffer for real rendering.
# Remove any stale X lock/socket from a previous run first. On a container
# *restart* (as opposed to a fresh recreate) /tmp persists, and a leftover
# /tmp/.X99-lock makes `Xvfb :99` refuse to start -> the game launches with no
# framebuffer and ALL vision breaks (screenshot, /video/stream, recorder all
# fail with "Cannot open display :99"). Clearing it makes restarts as robust as
# recreates.
echo "Clearing any stale X locks..."
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
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

# Set game rules via RCON. Opping is handled server-side
echo "Configuring game rules via RCON..."
python3 -c "
from mcrcon import MCRcon
try:
    with MCRcon('mc-server', 'mineclaude') as mcr:
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

# Persistent low-fps gameplay recorder (opt-in via RECORD_VIDEO=1). A second
# ffmpeg taps the same :99 framebuffer as /screenshot + /video/stream — x11grab
# is a read-only grab, so concurrent readers don't conflict. We start it here,
# after the world join + bridge are up, so segments capture actual gameplay
# rather than the loading screen. Output is 5-minute H.264 segments to
# /recordings (bind-mounted to ./state/video on the host); segmenting keeps each
# mp4 self-contained, so a `docker compose down` mid-write only loses the open
# segment and "the last 30 min" is just the newest 6 files. -g 25 = a keyframe
# every 5s (1500-frame segments split cleanly + scrubbable). Reuses
# MONITOR_VIDEO_FILTER (unset -> brighten default; empty -> no filter, matching
# the /video/stream semantics). Backgrounded + non-fatal: if ffmpeg can't start,
# it logs to /tmp/recorder.log and the container carries on.
if [ "${RECORD_VIDEO:-0}" = "1" ]; then
    mkdir -p /recordings
    REC_FILTER="${MONITOR_VIDEO_FILTER-eq=gamma=2.0:brightness=0.08:contrast=1.15}"
    REC_VF=()
    [ -n "$REC_FILTER" ] && REC_VF=(-vf "$REC_FILTER")
    echo "Starting gameplay recorder (5fps CRF28) -> /recordings"
    ffmpeg -nostdin -loglevel warning \
        -f x11grab -r 5 -video_size 854x480 -i :99 \
        "${REC_VF[@]}" \
        -c:v libx264 -preset veryfast -crf 28 -pix_fmt yuv420p -g 25 -an \
        -f segment -segment_time 300 -reset_timestamps 1 -strftime 1 \
        /recordings/play-%Y%m%d-%H%M%S.mp4 \
        > /tmp/recorder.log 2>&1 &
    echo "Recorder ffmpeg pid $! (log: /tmp/recorder.log)"
fi

# Keep container alive, streaming logs
echo "=== Entering log tail ==="
tail -f /tmp/hmc.log
