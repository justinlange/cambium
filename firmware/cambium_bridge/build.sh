#!/usr/bin/env bash
# Build/flash the cambium_bridge serial<->ESP-NOW modem (default: PowerFeather
# V2 / ESP32-S3, same board family as the fixture fleet).
#
#   ./build.sh                          # compile only (throwaway build dir)
#   ./build.sh --port /dev/ttyACM0      # compile + USB flash
#   ./build.sh --fqbn esp32:esp32:esp32s3   # any generic S3 board
#   ./build.sh --channel 11             # ESP-NOW channel build default
#
# The sketch has no board-specific code, so any ESP32-S3 FQBN works.

set -euo pipefail
cd "$(dirname "$0")"

FQBN="esp32:esp32:esp32s3_powerfeather"
PORT=""
CHANNEL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fqbn) FQBN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --channel) CHANNEL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

FLAGS=""
[[ -n "$CHANNEL" ]] && FLAGS+=" -DCB_CHANNEL=$CHANNEL"

# The powerfeather board def only exists in esp32 core >= 3.x; older cores
# (e.g. 2.0.7) fail with an opaque "board not found". Catch it up front.
if ! arduino-cli board details --fqbn "$FQBN" >/dev/null 2>&1; then
  echo "ERROR: FQBN '$FQBN' is not known to your installed esp32 core." >&2
  echo "  installed: $(arduino-cli core list | awk '/esp32:esp32/ {print $2}')" >&2
  echo "  esp32s3_powerfeather needs esp32 core >= 3.x. Fix one of:" >&2
  echo "    arduino-cli core upgrade esp32:esp32" >&2
  echo "    ./build.sh --fqbn esp32:esp32:esp32s3   # generic S3, works on 2.x" >&2
  exit 2
fi

# Unique build path per run: parallel compiles against Arduino's shared
# sketch cache corrupt artifacts (same convention as the fixture build.sh).
BUILD_PATH="$(mktemp -d "${TMPDIR:-/tmp}/cambium-bridge-build.XXXXXX")"
trap 'rm -rf "$BUILD_PATH"' EXIT

echo "compiling (fqbn: $FQBN, flags:${FLAGS:- none})"
arduino-cli compile --fqbn "$FQBN" \
  --build-property "compiler.cpp.extra_flags=$FLAGS" \
  --build-path "$BUILD_PATH" \
  .

BIN="$BUILD_PATH/cambium_bridge.ino.bin"
echo "artifact: $BIN ($(wc -c < "$BIN" | tr -d ' ') bytes)"
shasum -a 256 "$BIN"

if [[ -n "$PORT" ]]; then
  arduino-cli upload --fqbn "$FQBN" --port "$PORT" --build-path "$BUILD_PATH" .
fi
