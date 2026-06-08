#!/bin/bash
# Merge LWJGL linux-arm64 native .so files INTO the resolved natives-linux jars.
#
# Why merge instead of -Dorg.lwjgl.librarypath: LWJGL loads its own natives by
# extracting them from the natives-linux classifier jar on the classpath. Under
# HMC's launcher the librarypath override is honored, but in our standalone
# direct relaunch (raw java) LWJGL ignores it and falls back to extracting the
# x86_64 .so from the jar -> "LWJGL version <empty>" + a black/hung render on
# aarch64. Injecting the arm64 .so at the linux/arm64/... path INSIDE each
# natives-linux jar makes LWJGL extract the correct architecture with no JVM-arg
# tricks (this is the approach validated end-to-end in the spike).
#
# Requires the natives-linux jars to already exist under $LIBS (a launch/download
# must have resolved them first). Idempotent: re-running re-injects the same .so.
set -euo pipefail

LWJGL_VERSION="${LWJGL_VERSION:-3.3.3}"
BASE="https://repo1.maven.org/maven2/org/lwjgl"
LIBS="${LIBS:-/root/.minecraft/libraries/org/lwjgl}"
MODULES="lwjgl lwjgl-glfw lwjgl-jemalloc lwjgl-openal lwjgl-opengl lwjgl-stb lwjgl-tinyfd lwjgl-freetype"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

merged=0
for m in $MODULES; do
    x64="$LIBS/$m/$LWJGL_VERSION/$m-$LWJGL_VERSION-natives-linux.jar"
    if [ ! -f "$x64" ]; then
        echo "skip $m (no natives-linux jar at $x64)"
        continue
    fi
    arm="$tmp/$m.jar"
    curl -fL --retry 3 -o "$arm" "$BASE/$m/$LWJGL_VERSION/$m-$LWJGL_VERSION-natives-linux-arm64.jar"
    d="$tmp/$m"; mkdir -p "$d"
    (cd "$d" && unzip -oq "$arm" "linux/arm64/*")
    (cd "$d" && jar uf "$x64" linux/arm64)
    echo "merged arm64 natives into $m-$LWJGL_VERSION-natives-linux.jar"
    merged=$((merged + 1))
done
echo "Done ($merged jars merged)."
