#!/bin/sh
# Sendspin interop: the device's player against aiosendspin, the server
# library Music Assistant ships. Host-only; needs Docker. From the repo root:
#
#   sh device/tools/sendspin_interop/run.sh [version ...]
#
# Default versions: 10.2.1 (Music Assistant 2.10's pin, the 13-byte chunk
# header) and 9.1.1 (the 9-byte one). The player links libopus, so it is
# built with cgo.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
dev=$(cd "$here/../.." && pwd)
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT

docker run --rm -v "$dev/..:/src" -v "$out:/out" -w /src/device -e CGO_ENABLED=1 \
  golang:1.24 go build -o /out/interop ./tools/sendspin_interop
cp "$here/server.py" "$out/"
status=0
for v in ${*:-10.2.1 9.1.1}; do
  echo "#### aiosendspin $v"
  docker run --rm -v "$out:/w" -w /w python:3.13-slim sh -c \
    "pip -q --disable-pip-version-check install 'aiosendspin[server]==$v' 2>/dev/null && python -u server.py" || status=1
done
exit $status
