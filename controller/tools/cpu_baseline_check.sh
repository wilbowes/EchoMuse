#!/usr/bin/env bash
# Run a controller image under an emulated Proxmox `kvm64` CPU (#496).
#
#   controller/tools/cpu_baseline_check.sh <image>
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
# amd64 only: arm64 has no equivalent baseline problem here, and kvm64 is
# an x86 model.
set -euo pipefail

image="${1:?usage: $0 <image>}"
here="$(cd "$(dirname "$0")" && pwd)"
qemu="$(command -v qemu-x86_64-static || true)"
if [ -z "$qemu" ]; then
  echo "qemu-x86_64-static not found (apt-get install qemu-user-static)" >&2
  exit 2
fi

run() {
  docker run --rm --init --platform linux/amd64 \
    -v "$qemu:/qemu:ro" \
    -v "$here/cpu_baseline_probe.py:/probe.py:ro" \
    -w /app --entrypoint /qemu "$image" -cpu kvm64 "$@"
}

echo "== python stack under kvm64"
run /usr/local/bin/python3 /probe.py --kvm64

# ffmpeg is spawned as a subprocess, and qemu-user does not follow execve
# into a child — so the probe above cannot cover it. Decode a second of
# tone under the same CPU on its own.
echo "== ffmpeg under kvm64"
bytes="$(run /usr/bin/ffmpeg -hide_banner -loglevel error \
          -f lavfi -i sine=d=1 -f s16le -ar 48000 -ac 1 - | wc -c)"
if [ "$bytes" -ne 96000 ]; then
  echo "ffmpeg decoded $bytes bytes, expected 96000" >&2
  exit 1
fi
echo "ffmpeg decoded 1s of audio"
