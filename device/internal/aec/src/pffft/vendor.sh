#!/bin/sh
# Re-vendors PFFFT (Julien Pommier, FFTPACK licence, in pffft.c's header):
# pffft.c and pffft.h byte-identical to the pinned upstream commit. The echo
# canceller's FFT (../fftwrap_pffft.c); 65% of a speex frame was kiss_fft
# (2026-11-16), and pffft has a NEON path kiss lacks. Run from this directory.
set -eu
REV=0aec0327a6912e1a0ec5326eef737c2ce19bc836 # 2026-08-14
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
for f in pffft.c pffft.h; do
	curl -fsSL -o "$tmp/$f" "https://bitbucket.org/jpommier/pffft/raw/$REV/$f"
done
(cd "$tmp" && sha256sum -c -) <<SUMS
485f2c641b9bc9434720757307e825c2f694b682438da9052959ab1e445e1f16  pffft.c
d6ac7f26f7c3f87ed2ad7f0264c09d72285526d937b4dccc5fc1c97645a0d55d  pffft.h
SUMS
cp "$tmp/pffft.c" "$tmp/pffft.h" .
echo "pffft $REV vendored"
