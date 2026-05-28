#!/bin/bash
set -e
REPO_ROOT=$(git rev-parse --show-toplevel)
GIT_VERSION=$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null)
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

if docker run --rm \
  -e VERSION="$VERSION" \
  -e CGO_LDFLAGS="-Wl,--hash-style=both" \
  -e CGO_CFLAGS="$SUPPRESS" \
  -v "$(pwd)":/sdk \
  -v "$REPO_ROOT/GoTinyAlsa":/GoTinyAlsa \
  echomuse-compiler 2>/tmp/build_err.log; then
    echo ""
    echo "✓ Build succeeded → build/server"
    echo ""
else
    echo ""
    echo "✗ Build failed:"
    echo ""
    cat /tmp/build_err.log
    echo ""
    exit 1
fi
