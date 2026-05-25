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
echo "Building EchoMuse $VERSION"
docker run --rm \
  -e VERSION="$VERSION" \
  -e CGO_LDFLAGS="-Wl,--hash-style=both" \
  -e CGO_CFLAGS="-Wno-deprecated-declarations" \
  -v "$(pwd)":/sdk \
  -v "$REPO_ROOT/GoTinyAlsa":/GoTinyAlsa \
  echomuse-compiler
