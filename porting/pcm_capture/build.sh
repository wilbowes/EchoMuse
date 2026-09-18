#!/bin/sh
# Build pcm_capture for the Echo (ARMv7, FireOS sysroot) in the pinned
# compiler image — the same toolchain as the firmware, so it runs wherever
# the firmware does. Output: porting/pcm_capture/pcm_capture
set -eu
ROOT=$(git rev-parse --show-toplevel)
docker run --rm --entrypoint bash \
    -e CGO_LDFLAGS="-Wl,--hash-style=both" \
    -v "$ROOT/porting/pcm_capture":/sdk \
    -v "$ROOT/GoTinyAlsa":/GoTinyAlsa \
    echomuse-compiler \
    -c "cd /sdk && go build -tags server -o pcm_capture ."
file "$ROOT/porting/pcm_capture/pcm_capture" 2>/dev/null || true
