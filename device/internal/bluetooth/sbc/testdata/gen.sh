#!/bin/sh
# Regenerates the reference fixtures with libsbc's own tools (Debian
# bookworm's sbc-tools 2.0). Run from anywhere; needs docker.
#
#   in_<case>.au       the input signal (mk_input.py)
#   ref_<case>.sbc     sbcenc's frames for it, flags from cases.txt
#   refdec_<case>.au   sbcdec's decode of ref_<case>.sbc
#   ours_<case>.sbc    our encoder's frames, same configuration
#   oursdec_<case>.au  sbcdec's decode of ours_<case>.sbc
#
# Rerun after any change to the encoder; the tests compare our current output
# with ours_<case>.sbc and require the reference decode of it to match the
# input.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
device=$(cd "$here/../../../.." && pwd)
img=sbcref:local
if ! docker image inspect $img >/dev/null 2>&1; then
    docker run --name sbcref-build debian:bookworm sh -c \
        'apt-get update -qq && apt-get install -y -qq sbc-tools python3' >/dev/null
    docker commit sbcref-build $img >/dev/null
    docker rm sbcref-build >/dev/null
fi
docker run --rm -v "$here":/w -w /w $img sh -c '
    grep -v "^#" cases.txt | while read name rate ch flags; do
        python3 mk_input.py "$rate" "$ch" 1536 "in_$name.au"
        sbcenc $flags "in_$name.au" > "ref_$name.sbc"
        sbcdec -f "refdec_$name.au" "ref_$name.sbc"
    done'
docker run --rm -v "$device":/src -w /src golang:1.25 \
    sh -c 'SBC_DUMP=1 go test -count=1 -run TestDumpOurs ./internal/bluetooth/sbc/'
docker run --rm -v "$here":/w -w /w $img sh -c '
    grep -v "^#" cases.txt | while read name rate ch flags; do
        sbcdec -f "oursdec_$name.au" "ours_$name.sbc"
    done'
