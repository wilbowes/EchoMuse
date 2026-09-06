#!/bin/bash
# profile.sh — finish the reference question, then profile the speaker.
#
# Part A settles which channel actually reaches the mono driver: L=440 with R
# silent, then the reverse. If only one side is audible, that side is the
# correct AEC reference for any future stereo path.
#
# Part B measures the speaker's frequency response, which the Ch7/Ch8 discovery
# makes far easier than the controller-side rig that was planned. Stimulus and
# response arrive in ONE capture off ONE ADC clock — Ch7 is what went to the
# DAC, Ch6 is what the mic heard — so the transfer function needs no alignment
# step and no timing assumptions.
#
# TWO SWEEPS, AND THE FIRST THING PRINTED IS WHETHER THEY AGREE. If two sweeps
# minutes apart disagree, nothing downstream of the measurement is worth
# building, so the repeatability probe comes before any response table and not
# after it.
#
# Keep the room quiet for part B and do not move the device between sweeps.

set -u

OUT="${1:-$PWD/profile_results}"
HERE="$(cd "$(dirname "$0")" && pwd)"

restore() {
    echo ""
    echo "--- restoring device ---"
    adb shell "su -c 'start echomuse'" >/dev/null 2>&1
    echo "echomuse started (it re-seeds volume from startupVolume)"
}
trap restore EXIT INT TERM

adb get-state >/dev/null 2>&1 || { echo "No device over adb." >&2; exit 1; }
mkdir -p "$OUT"

# Signals are generated, not committed — 4MB of WAV that a dozen lines
# reproduce exactly.
if [ ! -f "$HERE/sweep.wav" ]; then
    echo "--- generating test signals ---"
    python3 "$HERE/generate_signals.py" || exit 1
fi

# capture_mics is built by ../build_tools.sh into device/build/.
CAPTURE_MICS="${CAPTURE_MICS:-$HERE/../../build/capture_mics}"
[ -f "$CAPTURE_MICS" ] || { echo "capture_mics not found at $CAPTURE_MICS —" >&2
                            echo "build it with device/tools/build_tools.sh" >&2
                            exit 1; }

echo "--- deploying ---"
adb push "$CAPTURE_MICS" /sdcard/capture_mics >/dev/null || exit 1
adb shell "su -c 'cp /sdcard/capture_mics /data/local/bin/ && chmod 755 /data/local/bin/capture_mics'"
for w in left_only right_only sweep; do
    adb push "$HERE/$w.wav" /data/local/tmp/$w.wav >/dev/null || exit 1
done

adb shell "su -c 'stop echomuse'"
sleep 2
adb shell "su -c 'tinymix -D 0 5 On; tinymix -D 0 56 On; tinymix -D 0 64 1 1'" >/dev/null
# Unity gain: loud enough to sit well above the room, and the highest setting
# at which the DAC is not contributing distortion of its own (1.5% THD at 127,
# 65% at 153 — see Volume in device/CLAUDE.md).
adb shell "su -c 'tinymix -D 0 61 127 127'" >/dev/null

capture() {   # capture <name> <wav> <seconds>
    local name=$1 wav=$2 secs=$3
    echo "  capturing $name ..."
    adb shell "su -c 'tinyplay /data/local/tmp/$wav.wav -D 0 -d 23 -p 2048 -n 4 >/dev/null 2>&1 &
                      sleep 0.5; /data/local/bin/capture_mics $secs'" >/dev/null 2>&1
    adb pull /data/local/tmp/capture.raw "$OUT/$name.raw" >/dev/null 2>&1 \
        || { echo "  capture failed" >&2; return 1; }
}

echo ""
echo "=== Part A — which channel reaches the driver ==="
capture "A_left_only"  left_only  5 || exit 1
capture "A_right_only" right_only 5 || exit 1

echo ""
python3 "$HERE/analyse_ref.py" "$OUT/A_left_only.raw"  --mode tone --label "A  L=440Hz, R silent"
python3 "$HERE/analyse_ref.py" "$OUT/A_right_only.raw" --mode tone --label "A  L silent, R=440Hz"
cat <<'A'
  Reading part A: compare the centre mic (ch6) RMS between the two. Audible in
  one and near-silent in the other means the hardware takes that side and
  discards the other, and that side is the reference to use. Similar in both
  means it sums them.
A

echo ""
echo "=== Part B — speaker profile ==="
echo "Two 10s sweeps, ~60s apart. Keep the room quiet and do not move the"
echo "device between them: the gap between the two IS the measurement of"
echo "whether any of this is worth building on."
echo ""
capture "B_sweep_1" sweep 12 || exit 1
echo "  waiting 60s before the second sweep ..."
sleep 60
capture "B_sweep_2" sweep 12 || exit 1

echo ""
python3 "$HERE/analyse_sweep.py" "$OUT/B_sweep_1.raw" --compare "$OUT/B_sweep_2.raw"
