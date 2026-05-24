#!/bin/bash
set -e
REPO_ROOT=$(git rev-parse --show-toplevel)
TOOL_DIR="$(pwd)/tools/capture_mics"
BUILD_DIR="$(pwd)/build"

echo "Building capture_mics diagnostic tool"

docker run --rm \
  --entrypoint bash \
  -e CGO_LDFLAGS="-Wl,--hash-style=both" \
  -v "$TOOL_DIR":/sdk \
  -v "$REPO_ROOT/GoTinyAlsa":/GoTinyAlsa \
  echomuse-compiler \
  -c "cd /sdk && go build -tags server -o capture_mics ."

mkdir -p "$BUILD_DIR"
mv "$TOOL_DIR/capture_mics" "$BUILD_DIR/capture_mics"

echo "Output: $BUILD_DIR/capture_mics"
echo ""
echo "Deploy:"
echo "  adb shell su -c 'stop echomuse'"
echo "  adb push $BUILD_DIR/capture_mics /sdcard/capture_mics"
echo "  adb shell \"su -c 'cp /sdcard/capture_mics /data/local/bin/capture_mics && chmod 755 /data/local/bin/capture_mics'\""
