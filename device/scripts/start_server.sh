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

# ── Wait for echoaudioservice (up to 4 minutes) ──────────────────────────────
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
trap 'kill $SERVER_PID $TRIM_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null; amp_off; exit 0' TERM INT

# ── Retry loop with auto-rollback ─────────────────────────────────────────────
attempt=0

while true; do
    START_TIME=$(date +%s)

    /data/local/bin/server >> /tmp/server.log 2>&1 &
    SERVER_PID=$!
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
        sleep 2
        continue
    fi

    attempt=$(( attempt + 1 ))
    echo "[start_server] Fast exit ${attempt}/${MAX_ATTEMPTS}: runtime=${RUNTIME}s exit=$EXIT_CODE" >> /tmp/server.log

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
            exit 1
            ;;
    esac

    # Verify fallback slot exists and is executable before committing
    if [ ! -x "/data/local/bin/$FALLBACK" ]; then
        echo "[start_server] Fallback slot $FALLBACK missing or not executable — cannot auto-rollback" >> /tmp/server.log
        exit 1
    fi

    echo "[start_server] Auto-rollback: $CURRENT → $FALLBACK after $MAX_ATTEMPTS failed starts" >> /tmp/server.log
    ln -sf "$FALLBACK" /data/local/bin/server

    # Exit cleanly — Android init will restart the service, now using $FALLBACK
    exit 0
done
