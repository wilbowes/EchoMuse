#!/usr/bin/env sh
# probe.sh — the ACTIVE half of porting: watch the stock firmware make sound,
# record the mics, and log the buttons. Run profile.sh first.
#
#     porting/probe.sh [-s <adb serial>] [-y] [--no-route] [--no-buttons]
#                      [--mics [--capture <pcm_capture binary>]] [output-dir]
#
# Unlike profile.sh this DOES act on the device, and says so before it starts:
#   route    presses volume up then volume down (net zero) so stock FireOS
#            plays its own chime, and snapshots the mixer, the codec registers
#            and the DAPM graph while it plays. Diffed against idle, that is
#            the route the vendor HAL sets up to make sound — the thing a
#            static profile cannot show.
#   buttons  logs input events while the operator presses each button.
#   mics     (opt-in) stops Android's mediaserver, which holds the mic,
#            records ~10s while the operator claps on cue, and starts it
#            again. Pushes one binary to /data/local/tmp and removes it.
#
# Rules this keeps:
#   - NEVER open a PCM mediaserver holds. The open does not fail, it blocks
#     forever (tinypcminfo did exactly that, 2026-09-18). Everything that
#     touches the mic runs after `stop media`.
#   - mediaserver is restarted on every exit path, Ctrl-C included, and a
#     reboot restores everything this changes.
#   - Refuses to run beside EchoMuse's own server, which holds the PCMs too.
set -eu

SERIAL=""; YES=0; ROUTE=1; BUTTONS=1; MICS=0; CAPTURE=""
while [ $# -gt 0 ]; do
  case "$1" in
    -s) SERIAL="$2"; shift 2 ;;
    -y) YES=1; shift ;;
    --no-route) ROUTE=0; shift ;;
    --no-buttons) BUTTONS=0; shift ;;
    --mics) MICS=1; shift ;;
    --capture) CAPTURE="$2"; shift 2 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
ADB="adb"; [ -n "$SERIAL" ] && ADB="adb -s $SERIAL"
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v adb >/dev/null || { echo "adb not found on PATH" >&2; exit 1; }
$ADB get-state >/dev/null 2>&1 || { echo "no device (adb get-state failed)" >&2; exit 1; }

PFX=""; SFX=""
isroot() { $ADB exec-out "$1" 2>/dev/null | grep -q 'uid=0('; }
if isroot "id"; then :
elif isroot "su -c id"; then PFX="su -c '"; SFX="'"
elif isroot "su 0 id"; then PFX="su 0 sh -c '"; SFX="'"
else echo "no root on the device — this needs it" >&2; exit 1; fi
dev() { $ADB exec-out "$PFX$1$SFX" 2>&1 | tr -d '\r\000' || true; }

model=$(dev "getprop ro.product.device" | tr -cd 'A-Za-z0-9_.-'); [ -n "$model" ] || model=unknown
OUT="${1:-./echomuse-probe-$model-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"
P="$OUT/probe.txt"; : > "$P"
say() { printf '%s\n' "$*" >&2; }
sec() { printf '\n===== %s\n' "$1" >> "$P"; }

# EchoMuse's server holds the PCMs; stock mediaserver is what we want to watch.
if dev "ls -l /proc/*/exe" | grep -q '/data/local/bin/server'; then
  say "EchoMuse's server is running on this device. Stop it first (stop echomuse),"
  say "or run this on a device still on its stock firmware."
  exit 1
fi

say ""
say "This will act on the device:"
[ $ROUTE = 1 ]   && say "  - press volume up then down (so it plays its chime; volume ends where it started)"
[ $BUTTONS = 1 ] && say "  - ask you to press each button once, and log what the kernel reports"
[ $MICS = 1 ]    && say "  - stop Android's media server for about 15s to record the mics, then start it again"
say "Nothing is installed. A reboot undoes anything this changes."
if [ $YES = 0 ]; then
  printf 'Continue? [y/N] ' >&2; read -r ans; case "$ans" in y|Y|yes) ;; *) exit 0 ;; esac
fi

{ echo "EchoMuse active probe"; echo "probe.sh format 1"; echo "host date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "build: $(dev "getprop ro.build.fingerprint")"; } >> "$P"

# Every PCM's state, one line each, so a snapshot shows what just opened.
PCMS='for f in /proc/asound/card*/pcm*/sub0/status; do s=closed; while read a b; do [ "$a" = "state:" ] && s=$b; done < $f; echo $f $s; done'

# ── Route: what the vendor HAL does to make sound ─────────────────────────
if [ $ROUTE = 1 ]; then
  say "Route: taking an idle snapshot…"
  sec "route: idle"
  dev "$PCMS" > "$OUT/pcm_idle.txt"
  dev "tinymix" > "$OUT/mixer_idle.txt"
  dev "for r in /sys/kernel/debug/regmap/*; do echo == \$r; cat \$r/registers; done" > "$OUT/regmap_idle.txt"
  cat "$OUT/pcm_idle.txt" >> "$P"

  # The chime is short but the HAL keeps the stream open for a few seconds
  # after it (standby delay), which is the window the snapshots land in.
  caught=0
  for attempt in keyevent manual; do
    if [ $attempt = keyevent ]; then
      say "Route: pressing volume up (the device should chime)…"
      dev "input keyevent 24" >/dev/null
    else
      say "No playback stream opened. Press a volume button on the device now (you have 15s)."
    fi
    tries=0; limit=$([ $attempt = keyevent ] && echo 12 || echo 30)
    while [ $tries -lt $limit ]; do
      snap=$(dev "$PCMS")
      if echo "$snap" | grep -E 'p/sub0/status RUNNING' >/dev/null; then
        echo "$snap" > "$OUT/pcm_playing.txt"
        dev "tinymix" > "$OUT/mixer_playing.txt"
        dev "for r in /sys/kernel/debug/regmap/*; do echo == \$r; cat \$r/registers; done" > "$OUT/regmap_playing.txt"
        dev "for w in /sys/kernel/debug/asoc/*/dapm/* /sys/kernel/debug/asoc/*/*/dapm/*; do [ -f \$w ] && { read l < \$w; echo \$w: \$l; }; done" > "$OUT/dapm_playing.txt"
        for f in $(grep -E 'p/sub0/status RUNNING' "$OUT/pcm_playing.txt" | cut -d' ' -f1); do
          echo "== $f" >> "$OUT/pcm_playing.txt"
          dev "cat ${f%status}hw_params" >> "$OUT/pcm_playing.txt"
        done
        caught=1; break
      fi
      tries=$((tries + 1)); sleep 0.5
    done
    [ $attempt = keyevent ] && dev "input keyevent 25" >/dev/null   # volume back down
    [ $caught = 1 ] && break
  done

  sec "route: playing"
  if [ $caught = 1 ]; then
    echo "playback PCM(s) that went RUNNING:" >> "$P"
    grep -E 'RUNNING|^==|channels|format|rate' "$OUT/pcm_playing.txt" >> "$P"
    echo >> "$P"; echo "mixer controls that changed (idle → playing):" >> "$P"
    diff "$OUT/mixer_idle.txt" "$OUT/mixer_playing.txt" | grep -E '^[<>]' >> "$P" || echo "(none)" >> "$P"
    echo >> "$P"; echo "registers that changed, by regmap (i2c bus-address, or the PMIC wrapper):" >> "$P"
    # Prefix each register with the regmap it belongs to, or a diff cannot
    # say which chip a line is from.
    for f in idle playing; do
      awk '/^== /{n=$2; sub(".*/","",n); next} {print n, $0}' "$OUT/regmap_$f.txt" > "$OUT/regmap_$f.lab"
    done
    diff "$OUT/regmap_idle.lab" "$OUT/regmap_playing.lab" | grep -E '^[<>]' >> "$P" || echo "(none)" >> "$P"
    rm -f "$OUT"/regmap_*.lab
    echo >> "$P"; echo "DAPM widgets powered while playing:" >> "$P"
    grep -E ': [^ ]+: On' "$OUT/dapm_playing.txt" >> "$P" || echo "(none)" >> "$P"
    say "Route: captured."
  else
    echo "no playback stream was seen RUNNING — route not captured" >> "$P"
    say "Route: not captured (see probe.txt)."
  fi
fi

# ── Buttons ───────────────────────────────────────────────────────────────
if [ $BUTTONS = 1 ]; then
  sec "buttons: devices"
  # Came back empty once on VVV and never again; one retry costs nothing.
  devs=$(dev "getevent -pl"); [ -n "$devs" ] || { sleep 1; devs=$(dev "getevent -pl"); }
  echo "${devs:-(getevent -pl returned nothing)}" >> "$P"
  say ""
  say "Buttons: press EACH button on the device once, slowly, about 2s apart."
  say "Write down the order you pressed them in. Recording for 25s…"
  say "(The action button may wake Alexa and the mute button mutes the mics —"
  say " press mute a second time at the end to unmute.)"
  $ADB exec-out "${PFX}getevent -lt${SFX}" > "$OUT/getevent.txt" 2>&1 &
  gp=$!
  sleep 25
  kill $gp 2>/dev/null || true
  dev "pkill getevent; killall getevent" >/dev/null 2>&1 || true
  sec "buttons: key events (press order as recorded)"
  tr -d '\r' < "$OUT/getevent.txt" | grep -E 'EV_KEY|EV_SW' >> "$P" || echo "(no key events)" >> "$P"
  say "Buttons: done. Note in the issue the order you pressed them in."
fi

# ── Mics ──────────────────────────────────────────────────────────────────
if [ $MICS = 1 ]; then
  sec "mics"
  # Who holds which capture PCM, and in what format, read while it is RUNNING —
  # the format the vendor uses is the one the hardware accepts.
  cap=$(dev "$PCMS" | grep -E 'c/sub0/status RUNNING' | cut -d' ' -f1 | sed -n 1p)
  if [ -z "$cap" ]; then
    echo "no capture PCM is running — nothing holds the mic to learn its format from" >> "$P"
    say "Mics: no running capture stream to copy the format from; skipping."
  else
    hw=$(dev "cat ${cap%status}hw_params")
    owner=$(dev "cat $cap" | sed -n 's/^owner_pid *: *//p')
    card=$(echo "$cap" | sed -E 's|.*/card([0-9]+)/.*|\1|'); pcmdev=$(echo "$cap" | sed -E 's|.*/pcm([0-9]+)c/.*|\1|')
    ch=$(echo "$hw" | sed -n 's/^channels: *//p'); rate=$(echo "$hw" | sed -n 's/^rate: *\([0-9]*\).*/\1/p')
    fmt=$(echo "$hw" | sed -n 's/^format: *//p' | tr 'A-Z' 'a-z')
    ownername=$(dev "cat /proc/$owner/cmdline" | tr '\0' ' ')
    { echo "capture: $cap (card $card device $pcmdev) held by pid $owner: $ownername"; echo "$hw"; } >> "$P"

    case "$ownername" in *mediaserver*) ;; *)
      echo "held by something other than mediaserver — not stopping it" >> "$P"
      say "Mics: the mic is held by '$ownername', not mediaserver. Not touching it."
      cap="" ;;
    esac
  fi

  if [ -n "$cap" ]; then
    tool=""
    case "$fmt" in
      s16_le|s32_le) tool="tinycap" ;;   # stock tinycap handles these
    esac
    if [ -z "$tool" ] && [ -n "$CAPTURE" ]; then tool="pcm_capture"; fi
    if [ -z "$tool" ]; then
      echo "format $fmt needs pcm_capture (stock tinycap cannot record it); pass --capture" >> "$P"
      say "Mics: this device records $fmt, which stock tinycap can't. Re-run with"
      say "      --capture <pcm_capture binary> (see porting/README.md)."
    else
      restore() { dev "start media; rm -f /data/local/tmp/em_mic.raw /data/local/tmp/pcm_capture" >/dev/null; }
      trap 'restore' EXIT INT TERM
      [ $tool = pcm_capture ] && $ADB push "$CAPTURE" /data/local/tmp/pcm_capture >/dev/null && dev "chmod 755 /data/local/tmp/pcm_capture" >/dev/null
      SECS=10
      say ""
      say "Mics: recording ${SECS}s. Stay QUIET until told to clap."
      dev "stop media" >/dev/null; sleep 1
      if [ $tool = pcm_capture ]; then
        $ADB exec-out "${PFX}/data/local/tmp/pcm_capture -D $card -d $pcmdev -c $ch -r $rate -f $fmt -t $SECS -o /data/local/tmp/em_mic.raw${SFX}" > "$OUT/capture.log" 2>&1 &
      else
        bits=$([ "$fmt" = s16_le ] && echo 16 || echo 32)
        $ADB exec-out "${PFX}tinycap /data/local/tmp/em_mic.raw -D $card -d $pcmdev -c $ch -r $rate -b $bits & p=\$!; sleep $SECS; kill -INT \$p${SFX}" > "$OUT/capture.log" 2>&1 &
      fi
      cp=$!
      sleep 4; say "  >>> CLAP ONCE, close to the FRONT of the device (the action button side)"
      sleep 3; say "  >>> CLAP ONCE, close to the BACK (opposite side)"
      wait $cp || true
      $ADB pull /data/local/tmp/em_mic.raw "$OUT/mics.raw" >/dev/null 2>&1 || true
      restore; trap - EXIT INT TERM
      say "Mics: done, media server restarted ($(dev "getprop init.svc.media"))."
      cat "$OUT/capture.log" >> "$P"
      raw="$OUT/mics.raw"
      # tinycap writes a WAV; skip its 44-byte header for the analysis.
      if [ $tool = tinycap ] && [ -s "$raw" ]; then tail -c +45 "$raw" > "$raw.pcm" && mv "$raw.pcm" "$raw"; fi
      if [ -s "$raw" ] && command -v python3 >/dev/null; then
        python3 "$HERE/analyse_mics.py" "$raw" "$ch" "$fmt" "$rate" 4.5 7.5 >> "$P" 2>&1 || true
      else
        echo "(no capture, or no python3 on this host to analyse it)" >> "$P"
      fi
    fi
  fi
fi

tar -czf "$OUT.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")"
say ""
say "Done: $OUT.tar.gz"
say "It holds no network or account data, but mics.raw is a recording of the"
say "room. Leave it out of a public post if anything but the claps was said."
