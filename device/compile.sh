#!/bin/bash
docker run --rm \
  -e CGO_LDFLAGS="-Wl,--hash-style=both" \
  -v "$(pwd)":/sdk \
  -v ~/GoTinyAlsa:/GoTinyAlsa \
  echomuse-compiler
