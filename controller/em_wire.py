"""
Constants of the device link that must match the Echo's side.

Frame codes, mic and speaker frame sizes, the voice pacing and prime windows
the device's buffers set, the WebSocket keepalive, and the features this
controller announces on the ack. Several are read by guards against the Go
source (tests/test_voice_pacing.py, test_capabilities.py), so a change here
is a change to the wire contract: docs/device-controller-interface.md.
"""

import em_ble_gatt

# Mic
CHUNK_BYTES          = 1280 * 2   # 2560 bytes = 80ms at 16kHz S16_LE mono
# NOTE: VOICE_PREROLL_DISCARD lives in em_esphome.py (esphome.VOICE_PREROLL_DISCARD)
# — it's used there in _stream_mic_audio, and _run_voice_locked below reads it
# via that single source of truth rather than keeping a second copy here that
# could drift out of sync (a duplicate here was previously dead code — see
# v2.6.3 changelog — resist the temptation to reintroduce it).

# Speaker — must match PcmSpeaker constants in Go. The wire carries MONO
# 48kHz (the device duplicates to stereo at the ALSA write — shipping two
# identical channels to a mono speaker doubled TTS bandwidth for nothing,
# and halving it matters on marginal 2.4GHz links).
SPEAKER_RATE   = 48000
SPEAKER_PERIOD = 2048
SPEAKER_BYTES  = SPEAKER_PERIOD * 2       # 4096 bytes/period (mono S16)
SPEAKER_PERIOD_SECONDS = SPEAKER_BYTES / (SPEAKER_RATE * 2)   # ~42.7ms

# How far ahead of realtime a VOICE response may be queued.
#
# The voice path used to send every period as fast as the socket would take
# it, with TCP backpressure as the only brake. That is what cut long
# responses off mid-sentence, and the chain is mechanical:
#
#   1. controller pushes ~1.7x realtime, bounded only by the link
#   2. the device's WS read goroutine calls PumpPeriod INLINE per 0x02 frame
#   3. pump() ends in a BLOCKING channel send, on a channel 128 periods
#      (~5.5s) deep
#   4. once full, the read goroutine blocks inside PumpPeriod and stops
#      calling ReadMessage
#   5. gorilla fires the pong handler only INSIDE ReadMessage, so a blocked
#      device cannot answer a ping
#   6. we ping every 20s and close after 10s without a pong
#   7. the buffer drains at realtime, so the block outlasts the timeout
#   8. 1011 keepalive ping timeout, mid-response
#
# Measured on Test Echo 1, 2026-08-30: 3.4MB (35.4s of audio) sent in 21.3s,
# 9.8s of that blocked in socket writes, connection closed 5s later. Short
# responses never reproduce it because they never fill 5.5s.
#
# SENDING FASTER THAN THIS BUYS NOTHING. The device can hold 5.5s and no
# more; everything beyond that sits in TCP buffers, which are lost on a
# reconnect exactly like un-sent audio is. So the excess never improved
# stall resilience — it only bought the block. Pacing to just under the
# device's own depth keeps the same protection with none of the cost.
#
# 4.0s to match em_player.LEAD_S, which reached the same number from the
# same constraint for the music plane: ~1.4s of headroom under audioChanDepth
# so the feed can never outrun the device's channel. Voice had no equivalent,
# and that asymmetry was the bug.
VOICE_LEAD_S = 4.0

# The device holds playback until ~this much audio is buffered (or EOS
# arrives) — primePeriods in pcm_speaker.go. The post-playback drain sleep
# must allow for the delayed start.
SPEAKER_PRIME_SECONDS = 1.1

# WebSocket keepalive on every device plane. The timeout is how long a ping may
# go unanswered before the link is declared dead, and 10s was shorter than the
# loss bursts this link actually has: with the BLE scan running, a Dot loses
# enough frames that TCP's retransmits alone can outlast it, and the overnight
# `1011 keepalive ping timeout` closes were exactly that (2026-09-23) — a live
# device torn down, its HA entities flapping, to rediscover it 5s later. 30s
# rides those out and still finds a dead Echo within ~50s; the device waits 45s
# for its own pongs (wsPongWait), so the two ends now disagree less.
WS_PING_INTERVAL_S = 20
WS_PING_TIMEOUT_S = 30

# Binary frame types
MIC_FRAME_TYPE     = 0x01
VAD_END_TYPE       = 0x04
# Distinct from VAD_END_TYPE — device never detected speech at all within its
# local no-speech grace period (see device/internal/client/data.go
# noSpeechTimeout), as opposed to VAD_END_TYPE which means speech was
# detected and then ended normally. Each frame type queues its matching
# string sentinel (esphome.VAD_SENTINEL_END / VAD_SENTINEL_TIMEOUT) so the
# type travels with the queue item — B5 fix, 2026-07-07; the old None +
# device.last_vad_was_timeout side-channel let a second sentinel overwrite
# the first's flag before it was consumed. OWW/barge-watcher consumers treat
# both flavours identically; esphome's _stream_mic_audio differentiates.
VAD_NO_SPEECH_TIMEOUT_TYPE = 0x05

# BLE advertisement batches from the device's passive scanner, moved off the
# control plane (#404). The control WebSocket is where liveness is measured —
# the RTT echo and the keepalive pong take the same mutex and the same TCP
# stream — so bulk telemetry there head-of-line-blocks the channel a device's
# health is judged on. Proven by crossover on 2026-09-01: the proxy moved
# between two Dots on one desk and took the excursions with it.
#
# Payload is the same JSON body the control message carried, so nothing about
# what an advert IS changed; only which socket it arrives on.
BLE_ADVERTS_TYPE   = 0x06

# What this controller can do, announced to the device on the ack. The mirror
# of the device's own `capabilities` in its register message, and it exists for
# the same reason: negotiate by capability, never by version string.
#
# `ble_adverts_data` says this controller reads BLE_ADVERTS_TYPE off the data
# plane. A device must not send those frames unnegotiated — an older controller
# ignores unknown frame types, so the adverts would vanish in silence, which is
# a worse fault than the one being fixed. The control-plane `ble_adverts`
# message stays handled forever, for firmware that predates this.
#
# `listen_session` says this controller understands private listening
# (docs/listening.md): listen_state, session-tagged oww_wake, 0x07 session
# audio and the listen_* replies. A device goes quiet only when it sees this —
# an older controller acts on a device wake only when stream frames arrive.
#
# "output_chain": this controller sends UNPROCESSED audio to a device that
# announced the same capability, and leaves EQ, bass guard and limiter to it.
# The device runs its chain only when it sees this, so neither half alone
# changes anything and the two can never both process the same audio.
#
# "ble_connect": this controller drives Bluetooth LE connections through a
# device that announced the same capability (#656), as BLE_GATT_TYPE frames in
# both directions. A message on a new frame code is negotiated both ways for
# the usual reason: sent to a side that does not read it, it vanishes.
CONTROLLER_FEATURES = ["ble_adverts_data", "listen_session", "output_chain",
                       "ble_connect"]
BLE_GATT_TYPE      = em_ble_gatt.FRAME_BLE_GATT
SPEAKER_FRAME_TYPE = 0x02
SPEAKER_EOS_TYPE   = 0x03
MIC_HEADER_LEN     = 3   # [type][seq_hi][seq_lo]
