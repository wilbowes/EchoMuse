#!/bin/bash
set -e

REPO_ROOT=$(git rev-parse --show-toplevel)
BUILD_DIR="$(pwd)/build"

build_tool() {
    local name=$1
    local tool_dir="$(pwd)/tools/$name"

    echo "Building $name..."
    docker run --rm \
        --entrypoint bash \
        -e CGO_LDFLAGS="-Wl,--hash-style=both" \
        -v "$tool_dir":/sdk \
        -v "$REPO_ROOT/GoTinyAlsa":/GoTinyAlsa \
        echomuse-compiler \
        -c "cd /sdk && go build -tags server -o $name ."

    mkdir -p "$BUILD_DIR"
    mv "$tool_dir/$name" "$BUILD_DIR/$name"
    echo "Output: $BUILD_DIR/$name"
}

build_tool capture_mics
build_tool bf_capture

echo ""
echo "Deploy:"
echo "  adb shell su -c 'stop echomuse'"
echo "  adb push $BUILD_DIR/capture_mics /sdcard/capture_mics"
echo "  adb push $BUILD_DIR/bf_capture /sdcard/bf_capture"
echo "  adb shell \"su -c 'cp /sdcard/capture_mics /data/local/bin/capture_mics && chmod 755 /data/local/bin/capture_mics'\""
echo "  adb shell \"su -c 'cp /sdcard/bf_capture /data/local/bin/bf_capture && chmod 755 /data/local/bin/bf_capture'\""
echo ""
echo "Run:"
echo "  adb shell su -c 'bf_capture --angle 330 --seconds 5'"
echo "  adb pull /tmp/ ."
