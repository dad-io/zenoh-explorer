#!/usr/bin/env bash
# Tests scripts/bundle-macos.sh on a fake binary. macOS only (needs plutil).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

printf '#!/bin/sh\necho fake\n' > "$tmp/some-binary"
chmod +x "$tmp/some-binary"

out="$("$here/bundle-macos.sh" "$tmp/some-binary" "$tmp/out" 1.2.3-rc.1 | tail -n1)"
app="$tmp/out/Zenoh Explorer.app"

[ "$out" = "$app" ] || { echo "FAIL: printed '$out', expected '$app'" >&2; exit 1; }
[ -x "$app/Contents/MacOS/zenoh-explorer" ] || { echo "FAIL: executable missing" >&2; exit 1; }
plutil -lint "$app/Contents/Info.plist" >/dev/null
v1="$(plutil -extract CFBundleShortVersionString raw "$app/Contents/Info.plist")"
v2="$(plutil -extract CFBundleVersion raw "$app/Contents/Info.plist")"
[ "$v1" = "1.2.3-rc.1" ] || { echo "FAIL: short version '$v1'" >&2; exit 1; }
[ "$v2" = "1.2.3-rc.1" ] || { echo "FAIL: bundle version '$v2'" >&2; exit 1; }
exe="$(plutil -extract CFBundleExecutable raw "$app/Contents/Info.plist")"
[ "$exe" = "zenoh-explorer" ] || { echo "FAIL: CFBundleExecutable '$exe'" >&2; exit 1; }

# The source asset must not be modified.
grep -q '<string>0.1.0</string>' "$here/../assets/Info.plist" || { echo "FAIL: assets/Info.plist was edited" >&2; exit 1; }

echo "bundle test passed"
