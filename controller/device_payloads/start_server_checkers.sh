#!/system/bin/sh
# EchoMuse start script for checkers (Echo Show 5 gen 1, LineageOS 18.1).
#
# Differences from the biscuit script:
#   - No Fire OS services to wait on or kill. There is no echoaudioservice,
#     no ledcontroller and no "mixer". Critically, LineageOS's audioserver is
#     left RUNNING: system_server blocks forever on media.audio_policy without
#     it and Watchdog then reboots the device. See internal/profile.
#   - No tinymix block. Mixer init lives in internal/profile so it stays in
#     step with the card/device constants instead of drifting in a shell script.
#   - No LED handling: this device has a screen, not a ring.
#
# Retry policy is unchanged: an exit inside MIN_RUNTIME counts as a failed
# start, and after MAX_ATTEMPTS consecutive fast exits the inactive A/B slot is
# restored so init restarts with the previous binary.

MAX_ATTEMPTS=3
MIN_RUNTIME=15

# ── Hardware init ─────────────────────────────────────────────────────────────
# p2p0 stays down for the same reason as on biscuit: it wakes the WiFi stack
# for no benefit here.
ip link set p2p0 down 2>/dev/null

# Prevent WiFi suspension.
echo "EchoMuse" > /sys/power/wake_lock 2>/dev/null

# ── Log size cap ──────────────────────────────────────────────────────────────
# /tmp is RAM-backed and everything below only appends, so cap it.
LOG=/data/local/tmp/server.log
MAX_LOG=5242880    # 5MB
KEEP_LOG=524288    # 512KB carried into server.log.1
(
    while true; do
        sleep 300
        SIZE=$(wc -c < "$LOG" 2>/dev/null)
        if [ -n "$SIZE" ] && [ "$SIZE" -gt $MAX_LOG ]; then
            tail -c $KEEP_LOG "$LOG" > "${LOG}.1" 2>/dev/null
            : > "$LOG"
            echo "[start_server] Log trimmed: ${SIZE} bytes" >> "$LOG"
        fi
    done
) &
TRIM_PID=$!

# ── Amp safety ────────────────────────────────────────────────────────────────
# On checkers, Ext_Speaker_Amp_Switch=Off is the *playing* state; putting it
# back to On silences the output stage when the daemon is not running. The
# server does this itself on SIGTERM via Profile.SilenceAmp, but SIGKILL and
# panic paths skip it. Idempotent.
amp_off() {
    tinymix -D 0 Ext_Speaker_Amp_Switch On 2>/dev/null
}

# Belt and braces: make sure Android's audio services are running.
#
# The daemon must never leave audioserver stopped on LineageOS. system_server
# blocks forever on media.audio_policy without it, Watchdog kills system_server
# after 60s, and that reboots the device: a ~75s bootloop. The profile no
# longer stops them, but an older binary in the inactive A/B slot might, so
# assert it here on every start too.
ensure_android_audio() {
    start vendor.audio-hal 2>/dev/null
    start audioserver 2>/dev/null
}
ensure_android_audio

# ── Signal handling ───────────────────────────────────────────────────────────
SERVER_PID=0
trap 'kill $SERVER_PID $TRIM_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null; amp_off; exit 0' TERM INT

# ── Retry loop with auto-rollback ─────────────────────────────────────────────
attempt=0

while true; do
    START_TIME=$(date +%s)

    /data/local/bin/server >> "$LOG" 2>&1 &
    SERVER_PID=$!
    wait $SERVER_PID
    EXIT_CODE=$?

    amp_off

    END_TIME=$(date +%s)
    RUNTIME=$(( END_TIME - START_TIME ))

    if [ $RUNTIME -ge $MIN_RUNTIME ]; then
        attempt=0
        echo "[start_server] Server ran ${RUNTIME}s before exit (code $EXIT_CODE), restarting" >> "$LOG"
        sleep 2
        continue
    fi

    attempt=$(( attempt + 1 ))
    echo "[start_server] Fast exit ${attempt}/${MAX_ATTEMPTS}: runtime=${RUNTIME}s exit=$EXIT_CODE" >> "$LOG"

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
            echo "[start_server] Unknown slot '$CURRENT', cannot auto-rollback, giving up" >> "$LOG"
            ensure_android_audio
            exit 1
            ;;
    esac

    if [ ! -x "/data/local/bin/$FALLBACK" ]; then
        echo "[start_server] Fallback slot $FALLBACK missing, cannot auto-rollback" >> "$LOG"
        ensure_android_audio
        exit 1
    fi

    echo "[start_server] Auto-rollback: $CURRENT → $FALLBACK after $MAX_ATTEMPTS failed starts" >> "$LOG"
    ln -sf "$FALLBACK" /data/local/bin/server
    exit 0
done
