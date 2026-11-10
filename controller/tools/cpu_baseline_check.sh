#!/usr/bin/env bash
# Run a controller image under the oldest CPU it has to start on (#496).
#
#   controller/tools/cpu_baseline_check.sh <image>          # amd64, kvm64
#   controller/tools/cpu_baseline_check.sh <image> arm64    # Cortex-A53
#
# WHY: numpy 2.4's x86_64 wheels need x86-64-v2 (SSE4.2, POPCNT), and on a
# CPU without it the add-on did not start at all. kvm64 is such a CPU and
# is what a lot of Home Assistant VMs on Proxmox carry. Nothing else in CI
# could see this: every runner, and every machine we test on, is newer.
# The fault is a property of a WHEEL's build flags, so any dependency can
# reintroduce it in a routine bump, with every other check green.
#
# HOW: qemu-user translates the image's own python3 with `-cpu kvm64`, so
# the whole process sees kvm64's CPUID and faults on anything past it. It
# runs inside `docker run` rather than a bare chroot because onnxruntime's
# cpuinfo aborts without /proc, which looks exactly like a CPU fault and
# is not one. The probe checks that the emulated CPU really lacks SSE4.2,
# so a qemu that ignored `-cpu` cannot produce a pass.
#
# `--init` is REQUIRED, not tidiness. Without it qemu is PID 1, and when
# the guest faults qemu reports it by sending ITSELF the signal — which the
# kernel ignores for PID 1. So the failure this script exists to catch did
# not fail: it hung at 0% CPU, and in CI that is a job sitting out its
# timeout rather than a red check. Found on the first negative-control run.
#
# arm64 is the same check under a Cortex-A53: ARMv8.0, the Pi 3, and the
# same feature set as the Pi 4's A72. aarch64 wheels mostly build for
# ARMv8.0 and pick newer instructions at run time, but nothing made that
# true here until this ran, and a wheel built for ARMv8.2 would stop a Pi
# exactly as numpy 2.4 stopped kvm64. qemu is an x86 binary on an x86
# runner, so it runs as the entrypoint of the arm64 image without binfmt.
set -euo pipefail

image="${1:?usage: $0 <image> [amd64|arm64]}"
arch="${2:-amd64}"
case "$arch" in
  amd64) qemu_name="qemu-x86_64-static";  cpu=kvm64 ;;
  arm64) qemu_name="qemu-aarch64-static"; cpu=cortex-a53 ;;
  *) echo "unknown arch '$arch' (amd64 or arm64)" >&2; exit 2 ;;
esac
here="$(cd "$(dirname "$0")" && pwd)"
qemu="$(command -v "$qemu_name" || true)"
if [ -z "$qemu" ]; then
  echo "$qemu_name not found (apt-get install qemu-user-static)" >&2
  exit 2
fi

# A multi-arch image is run by its per-architecture manifest digest. Pulled
# as repo@<index digest>, the local store holds one architecture under that
# name, so the arm64 pull after the kvm64 one refused with "cannot overwrite
# digest" (first main run, 2026-10-19). A local image such as CI's
# echomuse-controller:pr is not in a registry and is run as given.
ref="$image"
digest="$(docker buildx imagetools inspect "$image" --format \
  "{{range .Manifest.Manifests}}{{if eq .Platform.Architecture \"$arch\"}}{{.Digest}}{{end}}{{end}}" \
  2>/dev/null || true)"
if [ -n "$digest" ]; then
  repo="${image%@*}"
  repo="${repo%:*}"
  ref="$repo@$digest"
fi
echo "running $ref"

run() {
  docker run --rm --init --platform "linux/$arch" \
    -v "$qemu:/qemu:ro" \
    -v "$here/cpu_baseline_probe.py:/probe.py:ro" \
    -w /app --entrypoint /qemu "$ref" -cpu "$cpu" "$@"
}

echo "== python stack under $cpu"
run /usr/local/bin/python3 /probe.py "--$cpu"

# ffmpeg is spawned as a subprocess, and qemu-user does not follow execve
# into a child — so the probe above cannot cover it. Decode a second of
# tone under the same CPU on its own.
echo "== ffmpeg under $cpu"
bytes="$(run /usr/bin/ffmpeg -hide_banner -loglevel error \
          -f lavfi -i sine=d=1 -f s16le -ar 48000 -ac 1 - | wc -c)"
if [ "$bytes" -ne 96000 ]; then
  echo "ffmpeg decoded $bytes bytes, expected 96000" >&2
  exit 1
fi
echo "ffmpeg decoded 1s of audio"
