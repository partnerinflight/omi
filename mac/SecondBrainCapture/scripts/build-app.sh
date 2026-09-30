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
# A stable signing identity keeps macOS privacy grants and firewall rules across rebuilds;
# fall back to ad-hoc, which makes the system treat every build as a new app.
IDENTITY="${SBC_SIGN_IDENTITY:-SecondBrainCapture Local}"
if security find-identity -v -p codesigning | grep -qF "$IDENTITY"; then
  codesign --force --sign "$IDENTITY" "$APP"
else
  echo "warning: signing identity '$IDENTITY' not found; signing ad-hoc (permissions reset each rebuild)" >&2
  codesign --force --sign - "$APP"
fi
codesign --verify "$APP"
echo "$APP"
