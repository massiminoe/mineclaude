#!/bin/bash
# Downloads LWJGL 3.3.3 linux-arm64 native libraries (.so) into a flat dir.
#
# Minecraft 1.21.5 bundles LWJGL 3.3.3 but Mojang's version manifest only ships
# the `natives-linux` (x86_64) classifier. On arm64 those .so files are the
# wrong architecture and the game dies at GL init. We download the official
# `natives-linux-arm64` classifier jars from Maven Central and extract their
# .so files into one directory; the arm64 entrypoint then points LWJGL/the game
# JVM's `java.library.path` at this dir so the correct natives load. ~5 MB total.
#
# Must match the LWJGL version MC 1.21.5 uses (3.3.3). If you bump MC and the
# LWJGL version changes, update LWJGL_VERSION below.
set -euo pipefail

OUT_DIR="${1:-/opt/arm64-natives}"
LWJGL_VERSION="${LWJGL_VERSION:-3.3.3}"
BASE="https://repo1.maven.org/maven2/org/lwjgl"
# The modules MC 1.21.5 loads natives for.
MODULES="lwjgl lwjgl-glfw lwjgl-jemalloc lwjgl-openal lwjgl-opengl lwjgl-stb lwjgl-tinyfd lwjgl-freetype"

mkdir -p "$OUT_DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for m in $MODULES; do
    url="$BASE/$m/$LWJGL_VERSION/$m-$LWJGL_VERSION-natives-linux-arm64.jar"
    echo "Fetching $m ($LWJGL_VERSION) arm64 natives..."
    curl -fL --retry 3 -o "$tmp/$m.jar" "$url"
    # Each jar carries its .so under linux/arm64/...; extract just the binaries.
    (cd "$tmp" && unzip -oq "$m.jar" "linux/arm64/*.so" 2>/dev/null || true)
done

find "$tmp/linux/arm64" -name '*.so' -exec cp {} "$OUT_DIR/" \;

echo ""
echo "arm64 natives in $OUT_DIR:"
ls -1 "$OUT_DIR"/*.so
echo "Done ($(ls "$OUT_DIR"/*.so | wc -l | tr -d ' ') libraries)."
