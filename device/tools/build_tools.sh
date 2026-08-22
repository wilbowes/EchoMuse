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

# oww_probe is NOT a standalone module like the tools above: it imports
# internal/wakeword and internal/wakeword/ort, and Go's internal-package rule
# only permits that from inside github.com/wilbowes/EchoMuse. So it is part of
# the device module and builds with the whole module mounted. No -tags server —
# it touches no hardware bindings, only models and a fixture.
build_module_tool() {
    local name=$1

    echo "Building $name..."
    mkdir -p "$BUILD_DIR"
    docker run --rm \
        --entrypoint bash \
        -e CGO_LDFLAGS="-Wl,--hash-style=both" \
        -v "$(pwd)":/sdk \
        -v "$REPO_ROOT/GoTinyAlsa":/GoTinyAlsa \
        echomuse-compiler \
        -c "cd /sdk && go build -o build/$name ./tools/$name"

    echo "Output: $BUILD_DIR/$name"
}

build_tool capture_mics
build_tool bf_capture
build_tool sendspin_bench
build_module_tool oww_probe

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
echo ""
echo "oww_probe also needs its data pushed (models + fixture + the ONNX Runtime"
echo "library), and prints a pass/fail verdict plus the real CPU cost:"
echo "  adb push $BUILD_DIR/oww_probe /sdcard/oww_probe"
echo "  adb push melspectrogram.onnx embedding_model.onnx <classifier>.onnx /sdcard/"
echo "  adb push ../internal/wakeword/testdata/ort_fixture.bin /sdcard/"
echo "  adb push libonnxruntime.so /sdcard/            # armeabi-v7a, 12.3MB"
echo "  adb shell \"su -c 'cp /sdcard/oww_probe /sdcard/*.onnx /sdcard/ort_fixture.bin /sdcard/libonnxruntime.so /data/local/tmp/ && chmod 755 /data/local/tmp/oww_probe'\""
echo "  adb shell \"su -c '/data/local/tmp/oww_probe -classifier /data/local/tmp/<classifier>.onnx'\""
echo ""
echo "Without adb, push over the controller shell plane instead:"
echo "  python controller/tools/push_file.py <local> <remote>   (resumable)"
