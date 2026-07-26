#!/bin/bash
set -e
REPO_ROOT=$(git rev-parse --show-toplevel)
# --match 'v*' keeps controller-v* tags out of the device version
GIT_VERSION=$(git -C "$REPO_ROOT" describe --tags --match 'v*' --always --dirty 2>/dev/null)
# If the tree is dirty or there's no tag, append datetime-dev
if echo "$GIT_VERSION" | grep -q "dirty"; then
    VERSION="$(date +%Y%m%d-%H%M)-dev"
else
    VERSION="$GIT_VERSION"
fi
echo "Building EchoMuse $VERSION..."

# Suppress known harmless warnings from vendored C sources:
#   -Wno-null-dereference: rnnoise/rnn.c assert-style null checks
#   -Wno-deprecated-declarations: tinyalsa pcm_read/pcm_write
SUPPRESS="-Wno-deprecated-declarations -Wno-null-dereference"

# Build explicitly with --entrypoint bash so we control ldflags directly.
# Previously we relied on the base image entrypoint to use $VERSION, which
# is opaque. This embeds the version string into the binary at compile time
# so the device reports the correct version to the controller on connect.
# BuildUnix floors the TLS verification clock — an Echo can boot with a
# bogus date before NTP syncs, and cert NotBefore checks would otherwise
# strand it (see internal/client/tlscreds.go).
BUILD_UNIX=$(date +%s)
BUILD_CMD="cd /sdk && mkdir -p build && go build \
    -tags server \
    -ldflags \"-X github.com/wilbowes/EchoMuse/internal/client.Version=${VERSION} \
               -X github.com/wilbowes/EchoMuse/internal/client.BuildUnix=${BUILD_UNIX}\" \
    -o build/server ./cmd/"

# Compiler image. Defaults to the API 22 build for biscuit; checkers (Echo
# Show 5, LineageOS 18.1) needs API 30:
#   EM_COMPILER_IMAGE=echomuse-compiler-api30 ./compile.sh
COMPILER_IMAGE="${EM_COMPILER_IMAGE:-echomuse-compiler}"
echo "Compiler image: $COMPILER_IMAGE"

if docker run --rm \
  --entrypoint bash \
  -e CGO_LDFLAGS="-Wl,--hash-style=both" \
  -e CGO_CFLAGS="$SUPPRESS" \
  -v "$(pwd)":/sdk \
  -v "$REPO_ROOT/GoTinyAlsa":/GoTinyAlsa \
  "$COMPILER_IMAGE" \
  -c "$BUILD_CMD" 2>/tmp/build_err.log; then
    echo ""
    echo "✓ Build succeeded → build/server  ($VERSION)"
    echo ""
else
    echo ""
    echo "✗ Build failed:"
    echo ""
    cat /tmp/build_err.log
    echo ""
    exit 1
fi
