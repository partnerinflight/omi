#!/usr/bin/env bash
# Run the existing CV1 build in Nordic's supported native macOS environment.
# NCS_ROOT=/path/to/ncs/v2.9.0 NRFUTIL=/path/to/nrfutil ./build-cv1-macos.sh --wifi
set -euo pipefail

FW="$(cd "$(dirname "$0")/.." && pwd)"
NCS_ROOT="${NCS_ROOT:-$FW/v2.9.0}"
NRFUTIL="${NRFUTIL:-nrfutil}"

if [ "$(uname -s)" != Darwin ]; then
  echo "Use build-cv1-local.sh inside your NCS 2.9.0 environment on this platform." >&2
  exit 2
fi
if [ ! -f "$NCS_ROOT/.west/config" ]; then
  echo "NCS_ROOT must point to an initialized SDK 2.9.0 west workspace." >&2
  exit 2
fi
SDK_HEAD="$(git -C "$NCS_ROOT/nrf" rev-parse HEAD)"
SDK_TAG="$(git -C "$NCS_ROOT/nrf" rev-parse 'v2.9.0^{commit}')"
if [ "$SDK_HEAD" != "$SDK_TAG" ]; then
  echo "Expected the Nordic SDK v2.9.0 revision in $NCS_ROOT/nrf." >&2
  exit 2
fi
exec "$NRFUTIL" toolchain-manager launch --ncs-version v2.9.0 \
  --chdir "$NCS_ROOT" -- /bin/bash "$FW/scripts/build-cv1-local.sh" "$@"
