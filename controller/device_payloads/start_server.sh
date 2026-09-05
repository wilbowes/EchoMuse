#!/system/bin/sh
# EchoMuse start script — A/B slot aware with auto-rollback.
#
# Retry policy: if the server exits in under MIN_RUNTIME seconds,
# it counts as a failed start. After MAX_ATTEMPTS consecutive fast
# exits the inactive slot is restored via symlink and the script
# exits cleanly so Android init restarts it with the old binary.
#
# If the server runs for >= MIN_RUNTIME seconds, the attempt counter
# resets — this was a successful start that crashed later (operational
# failure, not deployment failure), so we just restart without rollback.

MAX_ATTEMPTS=3
MIN_RUNTIME=15   # seconds below which an exit is treated as a failed start

# ── Wait for echoaudioservice, but only where Android exists ─────────────────
#
# This wait is for Amazon's audio service to bring the codec up before we take
# it. On emOS there is no Android userspace and no echoaudioservice, so the
# loop can never succeed and always runs its full 120 seconds — measured as a
# 126s delay from boot to a working assistant, every boot, for a service that
# will never appear. The firmware configures the codec's DAPM routes itself
# now (internal/bindings/codec), so there is nothing to wait for there either.
#
# The test is whether ANDROID is running, not whether this is emOS: the same
# script has to be correct on both for as long as both are supported, and a
# build flag would mean two artifacts to keep in step. /dev/__properties__ is
# Android's property service — present on FireOS, absent without it — so it
# tests the thing the wait actually depends on.
if [ -e /dev/__properties__ ]; then
    i=0
    while [ $i -lt 120 ]; do
        pid=$(ps | grep echoaudio | grep -v grep)
        if [ -n "$pid" ]; then
            sleep 5
            break
        fi
        sleep 2
        i=$((i + 2))
    done
fi

# ── Hardware init ─────────────────────────────────────────────────────────────
ip link set p2p0 down

# Prevent WiFi suspension
echo "EchoMuse" > /sys/power/wake_lock

# Speaker mixer init
tinymix -D 0 56 On
tinymix -D 0 64 1 1
tinymix -D 0 88 On
tinymix -D 0 61 100 100

# Mic gain — equalised across all four ADCs (A/B/C/D)
tinymix -D 0 89 88 88
tinymix -D 0 92 40 40
tinymix -D 0 107 88 88
tinymix -D 0 110 40 40
tinymix -D 0 125 88 88
tinymix -D 0 128 40 40
tinymix -D 0 143 88 88
tinymix -D 0 146 40 40

kill $(ps | grep ledcontroller | grep -v grep) 2>/dev/null

# ── Log size cap ──────────────────────────────────────────────────────────────
# /tmp is RAM-backed and everything below only ever appends — without a cap
# server.log grows until reboot (45MB observed, 2026-07-07). Past MAX_LOG,
# keep the newest KEEP_LOG bytes in server.log.1 and truncate in place; the
# server's O_APPEND fd (from >>) just continues writing at the new EOF, so
# no restart is needed and total log footprint stays bounded at ~5.5MB.
LOG=/tmp/server.log
MAX_LOG=5242880    # 5MB
KEEP_LOG=524288    # 512KB carried into server.log.1
(
    while true; do
        sleep 300
        SIZE=$(wc -c < "$LOG" 2>/dev/null)
        if [ -n "$SIZE" ] && [ "$SIZE" -gt $MAX_LOG ]; then
            tail -c $KEEP_LOG "$LOG" > "${LOG}.1" 2>/dev/null
            : > "$LOG"
            echo "[start_server] Log trimmed: ${SIZE} bytes (tail kept in ${LOG}.1)" >> "$LOG"
        fi
    done
) &
TRIM_PID=$!

# ── Persistent supervisor log ────────────────────────────────────────────────
# Everything above logs to /tmp, which is RAM-backed and therefore wiped by
# the reboot you perform to recover from a device that will not come back.
# The evidence needed to diagnose a failed restart is destroyed by the act of
# recovering from it (learned the hard way 2026-08-01).
#
# So the SUPERVISOR's own decisions — and only those — also go to /data, which
# survives reboots and OTA slot flips. Not the server's output: that is 45MB a
# day and none of it is what you need. This is a handful of lines per boot.
#
# Timestamps are SECONDS SINCE BOOT, not wall clock. Echos boot with bogus
# clocks before NTP — the same reason TLS verification clamps to the firmware
# build time — and a boot-time log is precisely where the wall clock is least
# trustworthy. The wall clock is recorded alongside as a hint only.
SUP_LOG=/data/local/etc/echomuse/supervisor.log
SUP_MAX=65536      # 64KB cap — this must never be able to fill /data
SUP_KEEP=32768     # bytes retained when trimming

mkdir -p /data/local/etc/echomuse 2>/dev/null

sup_log() {
    # Trim BEFORE appending, so a crash-loop writing every few seconds can
    # never grow past the cap between checks.
    # -f guard, not just 2>/dev/null: the redirect failure is reported by the
    # shell before wc runs, so a missing file would print to stderr on the
    # first write of every boot.
    if [ -f "$SUP_LOG" ]; then
        SIZE=$(wc -c < "$SUP_LOG" 2>/dev/null)
    else
        SIZE=0
    fi
    if [ "$SIZE" -gt $SUP_MAX ]; then
        tail -c $SUP_KEEP "$SUP_LOG" > "${SUP_LOG}.tmp" 2>/dev/null
        mv "${SUP_LOG}.tmp" "$SUP_LOG" 2>/dev/null
    fi
    # Shell built-ins only. `cut` is not on this device's PATH — the first
    # version of this used it and logged an empty uptime, quietly losing the
    # one timestamp that is trustworthy at boot.
    UP=""
    if [ -r /proc/uptime ]; then
        read UP _REST < /proc/uptime 2>/dev/null
        UP=${UP%.*}
    fi
    echo "up=${UP}s wall=$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$SUP_LOG"
}

sup_log "boot slot=$(readlink /data/local/bin/server 2>/dev/null)"

# ── Amp safety ────────────────────────────────────────────────────────────────
# Mute + amp off whenever the server is not running. The server does this
# itself on SIGTERM (PcmSpeaker.Close), but SIGKILL/panic paths skip it —
# and an enabled amp on an idle DAC produces audible hiss for as long as
# the server is down (between OTA slots was the worst case). Idempotent;
# the server re-enables the amp in its own startup sequence.
amp_off() {
    tinymix -D 0 61 0 0 2>/dev/null
    tinymix -D 0 5 Off 2>/dev/null
}

# ── Signal handling ───────────────────────────────────────────────────────────
# Forward SIGTERM/SIGINT to the server subprocess so Android init can
# cleanly stop the service (exec is no longer used, so init signals us).
# Wait for the server to finish its own graceful shutdown, then amp_off
# as belt-and-braces. The log-trim loop dies with us too.
SERVER_PID=0
trap 'sup_log "term signalled — supervisor exiting"; kill $SERVER_PID $TRIM_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null; amp_off; exit 0' TERM INT

# ── Retry loop with auto-rollback ─────────────────────────────────────────────
attempt=0

while true; do
    START_TIME=$(date +%s)

    /data/local/bin/server >> /tmp/server.log 2>&1 &
    SERVER_PID=$!
    sup_log "start pid=$SERVER_PID slot=$(readlink /data/local/bin/server 2>/dev/null)"
    wait $SERVER_PID
    EXIT_CODE=$?

    # Server is down — silence the amp until the next start (or forever,
    # if this turns out to be the rollback/give-up path).
    amp_off

    END_TIME=$(date +%s)
    RUNTIME=$(( END_TIME - START_TIME ))

    if [ $RUNTIME -ge $MIN_RUNTIME ]; then
        # Ran long enough — not a deployment failure.
        # Reset counter and restart (handles operational crashes).
        attempt=0
        echo "[start_server] Server ran ${RUNTIME}s before exit (code $EXIT_CODE) — restarting" >> /tmp/server.log
        sup_log "exit pid=$SERVER_PID code=$EXIT_CODE ran=${RUNTIME}s — restarting"
        sleep 2
        continue
    fi

    attempt=$(( attempt + 1 ))
    echo "[start_server] Fast exit ${attempt}/${MAX_ATTEMPTS}: runtime=${RUNTIME}s exit=$EXIT_CODE" >> /tmp/server.log
    sup_log "fast-exit ${attempt}/${MAX_ATTEMPTS} code=$EXIT_CODE ran=${RUNTIME}s"

    if [ $attempt -lt $MAX_ATTEMPTS ]; then
        sleep 3
        continue
    fi

    # ── Auto-rollback ─────────────────────────────────────────────────────────
    CURRENT=$(readlink /data/local/bin/server 2>/dev/null)
    case "$CURRENT" in
        server_a) FALLBACK=server_b ;;
        server_b) FALLBACK=server_a ;;
        *)
            echo "[start_server] Unknown slot '$CURRENT' — cannot auto-rollback, giving up" >> /tmp/server.log
            sup_log "giving up — unknown slot '$CURRENT', supervisor exiting"
            exit 1
            ;;
    esac

    # Verify fallback slot exists and is executable before committing
    if [ ! -x "/data/local/bin/$FALLBACK" ]; then
        echo "[start_server] Fallback slot $FALLBACK missing or not executable — cannot auto-rollback" >> /tmp/server.log
        sup_log "giving up — fallback $FALLBACK missing, supervisor exiting"
        exit 1
    fi

    echo "[start_server] Auto-rollback: $CURRENT → $FALLBACK after $MAX_ATTEMPTS failed starts" >> /tmp/server.log
    ln -sf "$FALLBACK" /data/local/bin/server
    sup_log "rollback $CURRENT -> $FALLBACK after $MAX_ATTEMPTS fast exits, supervisor exiting for init to restart it"

    # Exit cleanly — Android init will restart the service, now using $FALLBACK
    exit 0
done
