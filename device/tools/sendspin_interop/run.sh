#!/bin/sh
# Sendspin interop: the device's player against aiosendspin 9.1.1, the server
# library Music Assistant ships. Host-only; needs Docker. From the repo root:
#
#   sh device/tools/sendspin_interop/run.sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
dev=$(cd "$here/../.." && pwd)
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT

docker run --rm -v "$dev/..:/src" -v "$out:/out" -w /src/device -e CGO_ENABLED=0 \
  golang:1.24 go build -o /out/interop ./tools/sendspin_interop
cp "$here/server.py" "$out/"
docker run --rm -v "$out:/w" -w /w python:3.13-slim sh -c \
  'pip -q --disable-pip-version-check install "aiosendspin[server]==9.1.1" 2>/dev/null && python -u server.py'
