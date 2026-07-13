package client

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"log"
	"math"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/internal/aec"
	"github.com/wilbowes/EchoMuse/internal/beamformer"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/processor"
	"github.com/wilbowes/EchoMuse/pkg/mic"
	"github.com/wilbowes/EchoMuse/pkg/speaker"
)

// ─── Binary frame types ───────────────────────────────────────────────────────

const (
	frameTypeMic     = byte(0x01)
	frameTypeSpeaker = byte(0x02)
	frameTypeEOS     = byte(0x03)
	frameTypeVADEnd  = byte(0x04)
	// frameTypeNoSpeechTimeout signals that the turn ended because no speech
	// was ever detected — distinct from frameTypeVADEnd (speech detected,
	// then ended). Sent when noSpeechTimeout elapses with active==false the
	// entire time. Distinguishing the two lets the controller treat "wake
	// word then silence" (Alexa-equivalent: quietly give up) differently
	// from "spoke, pipeline processed it, HA had nothing to say" — the two
	// cases were previously indistinguishable on the wire, which is also
	// why the controller had no way to short-circuit the former without
	// risking mishandling the latter.
	frameTypeNoSpeechTimeout = byte(0x05)
)

// ─── VAD constants ────────────────────────────────────────────────────────────

const (
	vadMicChannels   = 9
	vadByteSample    = 3
	vadFramePeriod   = 512
	vadBytePeriod    = vadFramePeriod * vadMicChannels * vadByteSample // 13824
	vadOwwChunkBytes = 1280 * 2                                        // 2560 bytes = 80ms

	// prerollPeriods is how many processed 32ms periods of pre-gate audio are
	// retained while the VAD gate is closed and flushed upstream the moment it
	// opens (~512ms at 16). Only applies to lockMic (bounded turn) streams —
	// the always-on wake stream is ungated and sends everything, so OWW
	// always sees a continuous stream. For turns, preroll gives STT the true
	// first phoneme instead of a hard splice at gate-open.
	prerollPeriods = 16

	// noSpeechTimeout bounds how long streamMic will wait for speech to
	// ever be detected after a turn starts. If active never becomes true
	// within this window, the turn ends via frameTypeNoSpeechTimeout rather
	// than sitting open indefinitely — mirrors Alexa's behaviour of giving
	// up quickly on a wake word followed by silence, rather than depending
	// on the upstream pipeline's own (much longer, HA VAD-driven) timeout.
	// Only guards the "never spoke" case; once active==true this deadline
	// no longer applies — the existing silenceMax hysteresis owns speech
	// end-of-turn detection from that point on.
	noSpeechTimeout = 5 * time.Second
)

// noSpeechTimeoutForTest overrides noSpeechTimeout when non-zero — set only
// from tests, to avoid needing a real 5s wait per test run. Left at its
// zero value in production; streamMic falls back to the real constant.
var noSpeechTimeoutForTest time.Duration

func effectiveNoSpeechTimeout() time.Duration {
	if noSpeechTimeoutForTest > 0 {
		return noSpeechTimeoutForTest
	}
	return noSpeechTimeout
}

func vadPeriodRMS(mono []byte) float64 {
	n := len(mono) / 2
	if n == 0 {
		return 0
	}
	var sum float64
	for i := 0; i < n; i++ {
		s := int16(binary.LittleEndian.Uint16(mono[i*2:]))
		f := float64(s) / 32768.0
		sum += f * f
	}
	return math.Sqrt(sum / float64(n))
}

// ─── DataClient ───────────────────────────────────────────────────────────────

// Beam lock request states — see DataClient.beamReq.
const (
	beamReqNone   int32 = 0
	beamReqLock   int32 = 1
	beamReqUnlock int32 = 2
)

type DataClient struct {
	deviceID string
	mic      mic.Subscribable
	spk      speaker.Speaker

	readyCh chan string

	micMu     sync.Mutex
	micActive bool
	micStopCh chan struct{}

	// beamReq carries a pending beam lock/unlock request from the control
	// plane to the mic streaming goroutine. Beamformer methods are not safe
	// to call from other goroutines (same reason beam.Unlock is deferred
	// inside streamMic rather than called from StopMic), so the control
	// handler only sets this flag; streamMic consumes it with Swap at the
	// top of each period. Lets the controller lock the beamformer onto the
	// speaker's perimeter mic mid-stream at wake detection — wake-triggered
	// turns don't restart the stream (P0-1), so without this they ran the
	// entire turn on ch6 omni and the mic array did nothing for them.
	beamReq int32

	conn   *websocket.Conn
	connMu sync.Mutex

	beam              *beamformer.Beamformer
	proc              *processor.Processor
	aec               *aec.Canceller
	onDirectionChange func(angle float64)
	directionMu       sync.Mutex

	// pipeMu serialises access to beam and proc, which hold unsynchronised
	// per-period state (reused analysis buffers, EWMA smoothers, AGC gain).
	// Both are normally touched by a single streamMic goroutine, but a
	// StopMic→StartMic pair (sent after every voice turn) spawns the
	// replacement while the old goroutine may still be draining a period or
	// two — the select on a closed stopCh vs a ready mic channel picks
	// randomly, so the old goroutine can run Process() concurrently with
	// the new one's Lock()/Process(). Uncontended outside that brief
	// overlap, so the cost is a no-op lock per 160ms batch.
	pipeMu sync.Mutex
}

// NewDataClient wires the mic/speaker pipeline. canceller is the shared AEC
// instance — its far-end side is fed by the speaker's echo tap; this client
// runs its near-end side on the mono mic stream. Disabled cancellers pass
// audio through untouched.
func NewDataClient(deviceID string, microphone mic.Subscribable, spk speaker.Speaker, canceller *aec.Canceller) *DataClient {
	return &DataClient{
		deviceID: deviceID,
		mic:      microphone,
		spk:      spk,
		readyCh:  make(chan string, 1),
		beam:     beamformer.New(),
		proc:     processor.New(),
		aec:      canceller,
	}
}

// OnDirectionChanged registers a callback invoked when the estimated dominant
// source direction changes. Called from the mic streaming goroutine — keep it fast.
func (d *DataClient) OnDirectionChanged(cb func(angle float64)) {
	d.directionMu.Lock()
	d.onDirectionChange = cb
	d.directionMu.Unlock()
}

func (d *DataClient) NotifyReady(serverAddr string) {
	select {
	case d.readyCh <- serverAddr:
	default:
		select {
		case <-d.readyCh:
		default:
		}
		d.readyCh <- serverAddr
	}
}

// RequestBeamLock asks the running mic stream to lock the beamformer onto
// the best perimeter mic (respecting BeamformingEnabled config). Safe to call
// from any goroutine; consumed by streamMic on its next period. A later
// request overwrites an unconsumed earlier one.
func (d *DataClient) RequestBeamLock() {
	atomic.StoreInt32(&d.beamReq, beamReqLock)
}

// RequestBeamUnlock asks the running mic stream to release the beam lock and
// return to ch6 omni. Safe to call from any goroutine.
func (d *DataClient) RequestBeamUnlock() {
	atomic.StoreInt32(&d.beamReq, beamReqUnlock)
}

func (d *DataClient) StartMic(lockMic bool) {
	d.micMu.Lock()
	defer d.micMu.Unlock()
	if d.micActive {
		log.Println("[data] StartMic: already active — ignoring")
		return
	}
	d.connMu.Lock()
	conn := d.conn
	d.connMu.Unlock()
	if conn == nil {
		log.Println("[data] StartMic: no connection yet")
		return
	}
	d.micActive = true
	d.micStopCh = make(chan struct{})
	go d.streamMic(conn, d.micStopCh, lockMic)
	log.Println("[data] Mic streaming started")
}

func (d *DataClient) StopMic() {
	d.micMu.Lock()
	defer d.micMu.Unlock()
	if !d.micActive {
		return
	}
	close(d.micStopCh)
	d.micActive = false
	// beam.Unlock() is deferred inside streamMic — always runs on the mic
	// goroutine, eliminating the data race with Process() and Lock().
	log.Println("[data] Mic streaming stopped")
}

func (d *DataClient) Run(ctx context.Context) error {
	var lastAddr string
	for {
		var addr string
		if lastAddr == "" {
			// No previous address — block until control signals us.
			select {
			case <-ctx.Done():
				return ctx.Err()
			case addr = <-d.readyCh:
			}
		} else {
			// Lost connection — retry same addr after 5s, or use new addr
			// immediately if control signals one (e.g. controller moved).
			select {
			case <-ctx.Done():
				return ctx.Err()
			case addr = <-d.readyCh:
			case <-time.After(5 * time.Second):
				addr = lastAddr
			}
		}
		// Drain any stale addr queued while we were connected.
		select {
		case addr = <-d.readyCh:
		default:
		}
		lastAddr = addr
		log.Printf("[data] Connecting to %s", addr)
		if err := d.connect(ctx, addr); err != nil && err != context.Canceled {
			log.Printf("[data] Connection lost: %v — retrying", err)
		}
	}
}

func (d *DataClient) connect(ctx context.Context, addr string) error {
	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, _, err := dialer.DialContext(ctx, "ws://"+addr+"/data", http.Header{})
	if err != nil {
		return err
	}
	defer conn.Close()

	identifyBytes, _ := json.Marshal(map[string]string{
		"type":      "identify",
		"device_id": d.deviceID,
	})
	// Send identify BEFORE publishing conn — same ordering fix as the control
	// client's register message. StartMic can fire independently of controller
	// timing (unmute calls StartMic(false) from the button goroutine); once
	// d.conn is visible, streamMic's sendFrame writes under connMu, and this
	// unlocked write racing it would be a concurrent write on the same
	// gorilla conn (panics).
	if err := conn.WriteMessage(websocket.TextMessage, identifyBytes); err != nil {
		return err
	}
	log.Printf("[data] Identified as %s", d.deviceID)

	d.connMu.Lock()
	d.conn = conn
	d.connMu.Unlock()

	defer func() {
		d.micMu.Lock()
		if d.micActive {
			close(d.micStopCh)
			d.micActive = false
		}
		d.micMu.Unlock()

		d.connMu.Lock()
		d.conn = nil
		d.connMu.Unlock()
	}()

	for {
		msgType, data, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		if msgType != websocket.BinaryMessage || len(data) == 0 {
			continue
		}
		switch data[0] {
		case frameTypeSpeaker:
			if len(data) > 1 && d.spk != nil {
				if err := d.spk.PumpPeriod(data[1:]); err != nil {
					log.Printf("[data] PumpPeriod error: %v", err)
				}
			}
		case frameTypeEOS:
			log.Println("[data] Speaker: end of stream")
			if d.spk != nil {
				d.spk.EndStream()
			}
		default:
			log.Printf("[data] Unknown binary frame type: 0x%02x", data[0])
		}
	}
}

// streamMic subscribes to the mic, runs the processing pipeline, and streams
// binary frames to the controller. The always-on wake stream (!lockMic) is
// ungated and AGC-free: every period is sent, continuously. Bounded turn
// streams (lockMic) keep the VAD gate, preroll ring, end-of-speech sentinels,
// and no-speech timer.
func (d *DataClient) streamMic(conn *websocket.Conn, stopCh <-chan struct{}, lockMic bool) {
	if d.mic == nil {
		log.Println("[data] streamMic: no mic")
		return
	}

	// Clear micActive on exit regardless of why we stopped — StopMic, stopCh
	// signal, or ALSA stream death. Without this, a mic death leaves micActive=true
	// and StartMic silently refuses to restart.
	//
	// Ownership check (2026-07-06): only clear micActive if this goroutine is
	// still the current stream. StopMic→StartMic in quick succession (the
	// controller sends that pair after every voice turn) spawns a replacement
	// goroutine while this one is still draining its last few periods;
	// without the check, this defer then stamped micActive=false over the
	// replacement's true, and the NEXT mic_start spawned a second concurrent
	// stream that no StopMic could ever reach (micStopCh no longer points at
	// it). Leaked gated streams are silent while idle but transmit during
	// speech — every utterance reached the controller twice (STT heard
	// "turn on on the on the office…") and their VADEnd sentinels cleared
	// the OWW chunk buffer, progressively killing wake detection until the
	// process restarted. d.micStopCh is compared against our own stopCh as
	// the identity token: they're equal only if no StartMic ran after us.
	defer func() {
		d.micMu.Lock()
		owner := d.micStopCh == stopCh
		if owner {
			d.micActive = false
		}
		d.micMu.Unlock()
		// Unlock the beam only while still the current stream: if a
		// replacement stream has already started (StopMic→StartMic pair),
		// the beam belongs to it — this goroutine's late Unlock would
		// otherwise land after the replacement's Lock() and silently drop
		// the new turn onto ch6 omni. The replacement's own exit unlocks
		// instead (Unlock on an unlocked beam is a no-op, so the wake
		// stream's unconditional unlock stays harmless).
		if owner {
			d.pipeMu.Lock()
			d.beam.Unlock()
			d.pipeMu.Unlock()
		}
		log.Println("[data] streamMic: exited")
	}()

	// Claim a clean beam: a superseded stream skips its unlock (see the
	// exit defer), so a lock left behind by the previous turn is released
	// here — otherwise a lockMic turn replaced by the wake stream would
	// leave the wake stream on the old turn's perimeter mic with the
	// baseline frozen. Fresh stream = fresh gain, same reasoning
	// (Processor.ResetAGC — without it, a gain crushed by TTS echo
	// persists into the next listening stream).
	d.pipeMu.Lock()
	d.beam.Unlock()
	if lockMic {
		lockSnap := config.Get().Snapshot()
		turnBeamEnabled := lockSnap.BeamformingEnabled != nil && *lockSnap.BeamformingEnabled
		d.beam.Lock(turnBeamEnabled)
	}
	d.proc.ResetAGC()
	d.pipeMu.Unlock()

	ch := d.mic.Subscribe()
	defer d.mic.Unsubscribe(ch)

	cfg := config.Get()

	speechCount := 0
	silenceCount := 0
	active := false
	everActive := false // true once active has been true at least once this turn
	buf := make([]byte, 0, vadOwwChunkBytes*4)
	// preroll ring — processed mono periods captured while the gate is
	// closed, oldest first. Flushed into buf at gate open, cleared while
	// active. Slices are retained (not copied): Process() returns a fresh
	// allocation each period, so nothing aliases them.
	preroll := make([][]byte, 0, prerollPeriods)
	var seqNum uint16
	var periodCount uint64 // periodic RMS diagnostic
	var lastClipped uint64 // clip count at last diag line

	// Memoized linear mic gain — recomputed only when the config dB value
	// changes (config push mid-stream). Sentinel forces computation on the
	// first period.
	gainDb := -1
	gainLin := 1.0

	sendFrame := func(payload []byte) {
		frame := make([]byte, 3+len(payload))
		frame[0] = frameTypeMic
		binary.BigEndian.PutUint16(frame[1:3], seqNum)
		seqNum++
		copy(frame[3:], payload)
		d.connMu.Lock()
		err := conn.WriteMessage(websocket.BinaryMessage, frame)
		d.connMu.Unlock()
		if err != nil {
			log.Printf("[data] streamMic: send error: %v", err)
		}
	}

	// noSpeechTimer fires if speech is never detected within noSpeechTimeout
	// of turn start. Stopped (and its channel drained) the instant active
	// first becomes true — from that point on, end-of-turn is entirely
	// owned by the existing silenceMax hysteresis below, same as before
	// this change.
	//
	// Only armed when lockMic is true. Per SETUP.md's mic_start semantics:
	// mic_start with no lock_mic is the permanent, always-on ch6/omni
	// wake-word listening stream (started once at connect, meant to run
	// indefinitely) — mic_start with lock_mic:true is a bounded voice turn
	// (post-wake-word or button press, perimeter mic locked for the turn's
	// duration). The no-speech timeout is only meaningful for the latter;
	// arming it unconditionally silently killed the permanent listening
	// stream after 5s of ordinary silence, with nothing to restart it,
	// breaking wake-word detection entirely until a button press happened
	// to re-enter streamMic fresh. Confirmed against real device logs
	// before this fix: "no speech detected within timeout" fired 5s after
	// every idle-listening Mic streaming started, with no corresponding
	// StartMic call to bring it back.
	//
	// When not armed, noSpeechTimerC stays nil — a nil channel blocks
	// forever in a select, which is the idiomatic Go way to permanently
	// disable a select case at zero runtime cost.
	var noSpeechTimer *time.Timer
	var noSpeechTimerC <-chan time.Time
	if lockMic {
		noSpeechTimer = time.NewTimer(effectiveNoSpeechTimeout())
		defer noSpeechTimer.Stop()
		noSpeechTimerC = noSpeechTimer.C
	}

	for {
		select {
		case <-stopCh:
			return

		case <-noSpeechTimerC:
			// Timer firing implies active was never true — if it had been,
			// this case would already be unreachable (timer stopped below).
			// Unreachable entirely when !lockMic, since noSpeechTimerC is
			// nil in that case and a nil channel never becomes ready.
			log.Println("[data] streamMic: no speech detected within timeout — ending turn")
			sendFrame([]byte{frameTypeNoSpeechTimeout})
			return

		case raw, ok := <-ch:
			if !ok {
				return
			}
			// Stop has priority: select picks randomly among ready cases,
			// so without this a closed stopCh racing a ready mic channel
			// keeps this goroutine draining periods alongside its
			// replacement stream.
			select {
			case <-stopCh:
				return
			default:
			}

			snap := cfg.Snapshot()
			threshold := snap.VadThreshold
			speechNeeded := snap.VadSpeechMs / 32
			silenceMax := snap.VadSilenceMs / 32
			if speechNeeded < 1 {
				speechNeeded = 1
			}
			if silenceMax < 1 {
				silenceMax = 1
			}

			beamAngle := float64(-1)
			if snap.BeamAngle != nil {
				beamAngle = *snap.BeamAngle
			}
			// AGC is forced off on the always-on wake stream (!lockMic).
			// Adaptive gain with persistent state on a stream that never
			// restarts is a rebaselining mechanism by construction: in a
			// room with steady background noise above vadThreshold, the
			// RMS gate calls every noisy period "speech", the release path
			// walks the gain up toward amplifying the noise floor, and the
			// fast attack then compresses the wake word's envelope
			// mid-utterance — depressing OWW scores. Wake word models
			// are trained level-diverse and don't need AGC. The config
			// toggle still governs bounded lockMic turns, which get a fresh
			// ResetAGC each stream.
			agcEnabled := lockMic && (snap.AgcEnabled == nil || *snap.AgcEnabled)

			if snap.MicGainDb != nil && *snap.MicGainDb != gainDb {
				gainDb = *snap.MicGainDb
				gainLin = math.Pow(10, float64(gainDb)/20.0)
				log.Printf("[data] mic gain: %ddB (linear %.2f)", gainDb, gainLin)
			}

			d.pipeMu.Lock()
			// Consume any pending beam lock/unlock request from the control
			// plane (wake detection → lock, turn end → unlock). Handled on
			// this goroutine because Beamformer methods aren't safe to call
			// from the control handler. Lock() no-ops if already locked or
			// if beamforming is disabled in config.
			switch atomic.SwapInt32(&d.beamReq, beamReqNone) {
			case beamReqLock:
				turnBeam := snap.BeamformingEnabled != nil && *snap.BeamformingEnabled
				d.beam.Lock(turnBeam)
			case beamReqUnlock:
				d.beam.Unlock()
			}

			mono, angle := d.beam.Process(raw, beamAngle, gainLin)
			clipped := d.beam.ClippedSamples()

			// AEC — subtract the speaker's own output (reference tapped at
			// the ALSA write, aligned by aecDelayMs) before anything
			// measures or gates the signal. No-op while aecEnabled=false.
			// (Has its own mutex — inside pipeMu only for lock ordering
			// simplicity; aec.mu is a leaf lock, no inversion possible.)
			mono = d.aec.Process(mono)

			// ── Processing pipeline ──────────────────────────────────────
			// VAD on raw beamformed output — pre-NS/AGC so threshold is
			// consistent regardless of gain state.
			//
			// vadThreshold is calibrated in pre-gain (acoustic) units —
			// the values validated in the v2.6.3 session predate the fixed
			// mic gain and stay meaningful across gain changes. mono is
			// post-gain, so scale the threshold up by the same factor
			// rather than requiring every stored config to be retuned in
			// lockstep with micGainDb.
			rms := vadPeriodRMS(mono)
			speech := rms >= threshold*gainLin

			// Periodic RMS diagnostic — every ~10 min, or within ~16s of
			// the mic gain clamping a sample (clipping is the one signal
			// that says micGainDb is too hot for the room, so it's
			// reported promptly; the %100 bound stops sustained clipping
			// becoming its own log flood). Was every 100 counts (~16s
			// measured on-device) while idle capture levels were being
			// characterised — that job is done (2026-07-07 fleet
			// analysis) and /tmp/server.log is RAM-backed and unrotated.
			if periodCount%3750 == 0 || (clipped != lastClipped && periodCount%100 == 0) {
				log.Printf("[data] VAD diag: rms=%.5f threshold=%.5f gain=%ddB clipped=%d gate=%v active=%v agc=%v",
					rms, threshold*gainLin, gainDb, clipped, speech, active, agcEnabled)
				lastClipped = clipped
			}
			periodCount++

			// AGC — lockMic turn streams only (see agcEnabled above). Pass
			// the speech flag so AGC release freezes during silence,
			// preventing noise floor amplification. When agcEnabled is
			// false Process passes mono through untouched and gain state is
			// frozen at whatever it last was. (RNNoise NS removed
			// 2026-07-12 — see internal/processor package comment.)
			mono = d.proc.Process(mono, agcEnabled, speech)
			d.pipeMu.Unlock()
			// ─────────────────────────────────────────────────────────────

			// Notify direction listener — non-blocking, keep it fast.
			// Only fire when angle is valid (beam locked).
			if angle >= 0 {
				d.directionMu.Lock()
				cb := d.onDirectionChange
				d.directionMu.Unlock()
				if cb != nil {
					cb(angle)
				}
			}

			// Ungated wake stream: the always-on (!lockMic) stream sends
			// every processed period, batched into 80ms chunks — no VAD
			// gate, no preroll, no end-of-speech sentinels. openwakeword
			// is a streaming model whose internal mel-spectrogram buffer
			// assumes continuous audio; feeding it VAD-gated bursts spliced
			// together (even with preroll) measurably depresses scores, and
			// an absolute RMS threshold is wrong in at least one room of
			// every home. Bandwidth is a non-issue: 16kHz mono S16 is
			// 32KB/s, ~12.5 frames/s at this chunk size — 6× smaller than
			// the TTS playback stream. Turn endpointing for wake-triggered
			// turns is owned controller-side (HA STT_VAD_END in esphome
			// mode, plus the controller's own no-speech timeout); the RMS
			// gate below now serves only bounded lockMic turns.
			if !lockMic {
				buf = append(buf, mono...)
				for len(buf) >= vadOwwChunkBytes {
					sendFrame(buf[:vadOwwChunkBytes])
					buf = buf[vadOwwChunkBytes:]
				}
				continue
			}

			if speech {
				silenceCount = 0
				if !active {
					speechCount++
					if speechCount >= speechNeeded {
						active = true
						// Gate open — flush the preroll ring ahead of the
						// current period so the controller receives ~500ms of
						// pre-onset context (and the true start of speech,
						// including periods consumed by the speechNeeded
						// count-up) instead of a hard splice at onset.
						for _, p := range preroll {
							buf = append(buf, p...)
						}
						preroll = preroll[:0]
						if !everActive {
							everActive = true
							// Speech has genuinely started — the no-speech
							// grace period no longer applies (if it was ever
							// armed; nil when !lockMic — see construction
							// above). Stop the timer; drain per
							// time.Timer.Stop's documented pattern in case
							// it raced and already fired.
							if noSpeechTimer != nil {
								if !noSpeechTimer.Stop() {
									select {
									case <-noSpeechTimerC:
									default:
									}
								}
							}
						}
					}
				}
			} else {
				speechCount = 0
				if active {
					silenceCount++
					if silenceCount >= silenceMax {
						active = false
						silenceCount = 0
						if len(buf) > 0 {
							pad := make([]byte, vadOwwChunkBytes-len(buf)%vadOwwChunkBytes)
							buf = append(buf, pad...)
							for len(buf) >= vadOwwChunkBytes {
								sendFrame(buf[:vadOwwChunkBytes])
								buf = buf[vadOwwChunkBytes:]
							}
							buf = buf[:0]
						}
						sendFrame([]byte{frameTypeVADEnd})
					}
				}
			}

			if active {
				buf = append(buf, mono...)
				for len(buf) >= vadOwwChunkBytes {
					sendFrame(buf[:vadOwwChunkBytes])
					buf = buf[vadOwwChunkBytes:]
				}
			} else {
				// Gate closed — keep the most recent periods for the next
				// gate open.
				if len(preroll) >= prerollPeriods {
					copy(preroll, preroll[1:])
					preroll = preroll[:prerollPeriods-1]
				}
				preroll = append(preroll, mono)
			}
		}
	}
}
