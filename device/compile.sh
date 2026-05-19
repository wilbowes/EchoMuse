#!/bin/bash
set -e

REPO_ROOT=$(git rev-parse --show-toplevel)
VERSION=$(git -C "$REPO_ROOT" describe --tags --always --dirty)

echo "Building EchoMuse $VERSION"

docker run --rm \
  -e VERSION="$VERSION" \
  -e CGO_LDFLAGS="-Wl,--hash-style=both" \
  -v "$(pwd)":/sdk \
  -v "$REPO_ROOT/GoTinyAlsa":/GoTinyAlsa \
  echomuse-compiler
