#!/bin/bash
set -euo pipefail

# Assembles a macOS .app bundle from a compiled binary.
# Usage: ./scripts/bundle-macos.sh [path-to-binary]
# Defaults to target/release/zenoh-explorer if no argument given.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BINARY="${1:-$PROJECT_ROOT/target/release/zenoh-explorer}"
APP_NAME="Zenoh Explorer"
BUNDLE_DIR="$PROJECT_ROOT/target/${APP_NAME}.app"

if [ ! -f "$BINARY" ]; then
    echo "Error: Binary not found at $BINARY"
    echo "Run 'cargo build --release' first."
    exit 1
fi

echo "Assembling ${APP_NAME}.app ..."

# Clean previous bundle
rm -rf "$BUNDLE_DIR"

# Create .app directory structure
mkdir -p "$BUNDLE_DIR/Contents/MacOS"
mkdir -p "$BUNDLE_DIR/Contents/Resources"

# Copy binary
cp "$BINARY" "$BUNDLE_DIR/Contents/MacOS/"

# Copy Info.plist
cp "$PROJECT_ROOT/assets/Info.plist" "$BUNDLE_DIR/Contents/"

# Copy icon if present
if [ -f "$PROJECT_ROOT/assets/ZenohExplorer.icns" ]; then
    cp "$PROJECT_ROOT/assets/ZenohExplorer.icns" "$BUNDLE_DIR/Contents/Resources/"
else
    echo "Warning: No icon found at assets/ZenohExplorer.icns (app will use default icon)"
fi

echo "Bundle created at: $BUNDLE_DIR"
