#!/bin/bash
# Downloads mod JARs for MC 1.21.5 Fabric into mc-client/mods/
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODS_DIR="$SCRIPT_DIR/mods"
mkdir -p "$MODS_DIR"

echo "Downloading Baritone v1.14.0 (Fabric 1.21.5)..."
curl -L -o "$MODS_DIR/baritone-api-fabric-1.14.0.jar" \
    "https://github.com/cabaletta/baritone/releases/download/v1.14.0/baritone-api-fabric-1.14.0.jar"

echo "Downloading Minescript 5.0b11 (Fabric 1.21.5)..."
curl -L -o "$MODS_DIR/minescript-fabric-1.21.5-5.0b11.jar" \
    "https://cdn.modrinth.com/data/KcpXWngB/versions/7s2EWsqH/minescript-fabric-1.21.5-5.0b11.jar"

echo "Downloading Fabric API 0.128.2 (Fabric 1.21.5)..."
curl -L -o "$MODS_DIR/fabric-api-0.128.2+1.21.5.jar" \
    "https://cdn.modrinth.com/data/P7dR8mSH/versions/kKEGlsne/fabric-api-0.128.2%2B1.21.5.jar"

echo "Downloading hmc-specifics 2.3.0 (Fabric 1.21.5)..."
curl -L -o "$MODS_DIR/hmc-specifics-1.21.5-2.3.0-fabric-release.jar" \
    "https://github.com/3arthqu4ke/hmc-specifics/releases/download/2.3.0/hmc-specifics-1.21.5-2.3.0-fabric-release.jar"

echo ""
echo "Downloaded mods:"
ls -lh "$MODS_DIR"/*.jar
echo ""
echo "Done. Pyjinn (Minescript's Python engine) is bundled — no separate install needed."
