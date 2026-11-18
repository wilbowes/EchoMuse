#!/system/bin/sh
set -eu
# Cold boot: ueventd may still be preparing ALSA when the network service starts.
# Bound this to ten probes; Init retries the service if the device stays unavailable.
ready=
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if /system/bin/tinymix -D 0 'PCM Playback Volume' >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
[ -n "$ready" ] || exit 1

# The controller's OTA writes server_a/server_b into /data/local/bin, flips the
# `server` symlink there and kills the server (controller/em_ota.py). Its first
# run copies whatever that symlink names into slot A, so until then the symlink
# names the image's binary. Best-effort: without it this is the image's binary
# under Init, as before.
IMAGE=/data/echomuse/server
SLOTS=/data/local/bin
[ -e "$SLOTS/server" ] || { mkdir -p "$SLOTS" && ln -s "$IMAGE" "$SLOTS/server"; } 2>/dev/null || true

# run_slot returns once the binary behind the symlink has exited within 15s
# three times running: a failed update, counted as start_server.sh counts it.
# An exit after a longer run (the OTA's kill included) restarts it.
run_slot() {
    fast=0
    while [ "$fast" -lt 3 ]; do
        started=$(date +%s)
        /system/bin/logwrapper "$SLOTS/server" || true
        if [ $(($(date +%s) - started)) -ge 15 ]; then
            fast=0
        else
            fast=$((fast + 1))
        fi
        sleep 2
    done
}

if [ -x "$SLOTS/server" ]; then
    run_slot
    # Init alone would restart a broken update forever: try the other slot.
    case "$(readlink "$SLOTS/server")" in
        server_a) other=server_b ;;
        server_b) other=server_a ;;
        *) other= ;;
    esac
    if [ -n "$other" ] && [ -x "$SLOTS/$other" ]; then
        ln -sf "$other" "$SLOTS/server"
        run_slot
    fi
fi
# No slot, or neither stays up: the image's own binary, supervised by Init.
exec /system/bin/logwrapper "$IMAGE"
