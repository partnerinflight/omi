#!/usr/bin/env bash
# Build SecondBrainCapture.app (ad-hoc signed) into mac/SecondBrainCapture/build/.
set -euo pipefail
PKG="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PKG"
swift build -c release
BIN="$(swift build -c release --show-bin-path)/SecondBrainCapture"
APP="$PKG/build/SecondBrainCapture.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN" "$APP/Contents/MacOS/SecondBrainCapture"
cp "$PKG/Resources/Info.plist" "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
codesign --verify "$APP"
echo "$APP"
