#!/bin/bash
set -euo pipefail

# Assembles a macOS .app bundle from a compiled binary.
# Usage: ./scripts/bundle-macos.sh [binary] [out-dir] [version]
#   binary   default: target/release/zenoh-explorer
#   out-dir  default: target
#   version  default: package version from `cargo metadata`
# Prints the bundle path as the last line of stdout.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BINARY="${1:-$PROJECT_ROOT/target/release/zenoh-explorer}"
OUT_DIR="${2:-$PROJECT_ROOT/target}"
VERSION="${3:-}"
APP_NAME="Zenoh Explorer"
EXECUTABLE="zenoh-explorer" # must equal CFBundleExecutable in assets/Info.plist

if [ ! -f "$BINARY" ]; then
    echo "Error: Binary not found at $BINARY" >&2
    echo "Run 'cargo build --release' first." >&2
    exit 1
fi

if [ -z "$VERSION" ]; then
    VERSION="$(cd "$PROJECT_ROOT" && cargo metadata --no-deps --format-version 1 | jq -r '.packages[0].version')"
fi

mkdir -p "$OUT_DIR"
BUNDLE_DIR="$(cd "$OUT_DIR" && pwd)/${APP_NAME}.app"

echo "Assembling ${APP_NAME}.app (version ${VERSION}) ..." >&2
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR/Contents/MacOS" "$BUNDLE_DIR/Contents/Resources"

cp "$BINARY" "$BUNDLE_DIR/Contents/MacOS/$EXECUTABLE"
chmod +x "$BUNDLE_DIR/Contents/MacOS/$EXECUTABLE"

cp "$PROJECT_ROOT/assets/Info.plist" "$BUNDLE_DIR/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$BUNDLE_DIR/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$VERSION" "$BUNDLE_DIR/Contents/Info.plist"

if [ -f "$PROJECT_ROOT/assets/ZenohExplorer.icns" ]; then
    cp "$PROJECT_ROOT/assets/ZenohExplorer.icns" "$BUNDLE_DIR/Contents/Resources/"
else
    echo "Warning: No icon found at assets/ZenohExplorer.icns (app will use default icon)" >&2
fi

echo "$BUNDLE_DIR"
