#!/usr/bin/env bash
#
# Build the Omi CV1 local-only recorder firmware.
#
#   scripts/build-cv1-local.sh            # BLE-only recorder (dump with omi-local pull)
#   scripts/build-cv1-local.sh --wifi     # + Wi-Fi upload to a local receiver while charging
#
# Needs an nRF Connect SDK v2.9.0 west workspace (see BUILD_AND_OTA_FLASH.md or
# scripts/ci/build-cv1.sh) with `west` on PATH and the Zephyr SDK installed.
# Outputs: <build dir>/dfu_application.zip, merged.hex, merged_CPUNET.hex
set -euo pipefail

FW="$(cd "$(dirname "$0")/.." && pwd)"
BOARD=omi/nrf5340/cpuapp
BUILD_DIR="${BUILD_DIR:-$FW/build/local}"
EXTRA=()

if [ "${1:-}" = "--wifi" ]; then
  BUILD_DIR="${BUILD_DIR%/local}/local-wifi"
  EXTRA=(-DEXTRA_CONF_FILE=overlay-wifi-upload.conf -DSB_EXTRA_CONF_FILE=sysbuild-wifi.conf)
fi

west build -b "$BOARD" "$FW/omi" --sysbuild -d "$BUILD_DIR" --pristine always \
  -- -DBOARD_ROOT="$FW" -DCONF_FILE=omi.conf "${EXTRA[@]}"

test -s "$BUILD_DIR/dfu_application.zip"
test -s "$BUILD_DIR/merged.hex"
test -s "$BUILD_DIR/merged_CPUNET.hex"
echo "artifacts in $BUILD_DIR:"
ls -l "$BUILD_DIR/dfu_application.zip" "$BUILD_DIR/merged.hex" "$BUILD_DIR/merged_CPUNET.hex"
