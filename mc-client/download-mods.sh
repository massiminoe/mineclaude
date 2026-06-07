#!/bin/bash
# Downloads mod JARs for MC 1.21.5 Fabric into mc-client/mods/
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# MODS_DIR is overridable so the Dockerfile can run this same script (the single
# source of truth for the jar list + versions) targeting an in-image path.
MODS_DIR="${MODS_DIR:-$SCRIPT_DIR/mods}"
mkdir -p "$MODS_DIR"

echo "Downloading Baritone v1.14.0 (Fabric 1.21.5)..."
curl -fL --retry 3 -o "$MODS_DIR/baritone-api-fabric-1.14.0.jar" \
    "https://github.com/cabaletta/baritone/releases/download/v1.14.0/baritone-api-fabric-1.14.0.jar"

echo "Downloading Fabric API 0.128.2 (Fabric 1.21.5)..."
curl -fL --retry 3 -o "$MODS_DIR/fabric-api-0.128.2+1.21.5.jar" \
    "https://cdn.modrinth.com/data/P7dR8mSH/versions/kKEGlsne/fabric-api-0.128.2%2B1.21.5.jar"

echo "Downloading hmc-specifics 2.3.0 (Fabric 1.21.5)..."
curl -fL --retry 3 -o "$MODS_DIR/hmc-specifics-1.21.5-2.3.0-fabric-release.jar" \
    "https://github.com/3arthqu4ke/hmc-specifics/releases/download/2.3.0/hmc-specifics-1.21.5-2.3.0-fabric-release.jar"

# fabric-language-kotlin: required at runtime by the mineclaude native bridge
# mod (mc-mod/), which is written in Kotlin. The bridge mod itself is built
# in-Docker by the multi-stage build; this dep is downloaded here because it
# ships as a separate mod jar in the mods folder.
echo "Downloading fabric-language-kotlin 1.13.11+kotlin.2.3.21 (Fabric 1.21.5)..."
curl -fL --retry 3 -o "$MODS_DIR/fabric-language-kotlin-1.13.11+kotlin.2.3.21.jar" \
    "https://cdn.modrinth.com/data/Ha28R6CL/versions/2i87JpYj/fabric-language-kotlin-1.13.11%2Bkotlin.2.3.21.jar"

echo ""
echo "Downloaded mods:"
ls -lh "$MODS_DIR"/*.jar
echo ""
echo "Done."
