#!/bin/bash
# Downloads mod JARs for MC 1.21.5 Fabric into mc-client/mods/
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODS_DIR="$SCRIPT_DIR/mods"
mkdir -p "$MODS_DIR"

echo "Downloading Baritone v1.14.0 (Fabric 1.21.5)..."
curl -L -o "$MODS_DIR/baritone-api-fabric-1.14.0.jar" \
    "https://github.com/cabaletta/baritone/releases/download/v1.14.0/baritone-api-fabric-1.14.0.jar"

# Minescript: we ship a patched custom build, NOT the upstream Modrinth JAR.
# The fork lives at massiminoe/minescript@mc1.21.5-containers and adds:
#   - PR #40 container_* APIs
#   - A1 length-prefixed RPC framing (eliminates the stdout-writer race)
# The JAR is committed in mods/ alongside this script. To rebuild:
#   git clone -b mc1.21.5-containers git@github.com:massiminoe/minescript.git
#   cd minescript
#   NO_MINESCRIPT_FORGE_BUILD=1 NO_MINESCRIPT_NEOFORGE_BUILD=1 ./gradlew :fabric:build -x test
#   cp fabric/build/libs/minescript-fabric-1.21.5-5.0b11.jar <repo>/mc-client/mods/
echo "Minescript: using patched custom build already in mods/ (do not pull from Modrinth)."

echo "Downloading Fabric API 0.128.2 (Fabric 1.21.5)..."
curl -L -o "$MODS_DIR/fabric-api-0.128.2+1.21.5.jar" \
    "https://cdn.modrinth.com/data/P7dR8mSH/versions/kKEGlsne/fabric-api-0.128.2%2B1.21.5.jar"

echo "Downloading hmc-specifics 2.3.0 (Fabric 1.21.5)..."
curl -L -o "$MODS_DIR/hmc-specifics-1.21.5-2.3.0-fabric-release.jar" \
    "https://github.com/3arthqu4ke/hmc-specifics/releases/download/2.3.0/hmc-specifics-1.21.5-2.3.0-fabric-release.jar"

# fabric-language-kotlin: required at runtime by the mineclaude native bridge
# mod (mc-mod/), which is written in Kotlin. The bridge mod itself is built
# in-Docker by the multi-stage build; this dep is downloaded here because it
# ships as a separate mod jar in the mods folder.
echo "Downloading fabric-language-kotlin 1.13.11+kotlin.2.3.21 (Fabric 1.21.5)..."
curl -L -o "$MODS_DIR/fabric-language-kotlin-1.13.11+kotlin.2.3.21.jar" \
    "https://cdn.modrinth.com/data/Ha28R6CL/versions/2i87JpYj/fabric-language-kotlin-1.13.11%2Bkotlin.2.3.21.jar"

echo ""
echo "Downloaded mods:"
ls -lh "$MODS_DIR"/*.jar
echo ""
echo "Done. Pyjinn (Minescript's Python engine) is bundled — no separate install needed."
