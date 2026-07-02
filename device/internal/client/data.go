package client

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"log"
	"math"
	"net/http"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/internal/beamformer"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/processor"
	"github.com/wilbowes/EchoMuse/pkg/mic"
	"github.com/wilbowes/EchoMuse/pkg/speaker"
)

// ─── Binary frame types ───────────────────────────────────────────────────────

const (
	frameTypeMic             = byte(0x01)
	frameTypeSpeaker         = byte(0x02)
	frameTypeEOS              = byte(0x03)
	frameTypeVADEnd          = byte(0x04)
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

type DataClient struct {
	deviceID string
	mic      mic.Subscribable
	spk      speaker.Speaker

	readyCh chan string

	micMu     sync.Mutex
	micActive bool
	micStopCh chan struct{}

	conn   *websocket.Conn
	connMu sync.Mutex

	beam              *beamformer.Beamformer
	proc              *processor.Processor
	onDirectionChange func(angle float64)
	directionMu       sync.Mutex
}

func NewDataClient(deviceID string, microphone mic.Subscribable, spk speaker.Speaker) *DataClient {
	return &DataClient{
		deviceID: deviceID,
		mic:      microphone,
		spk:      spk,
		readyCh:  make(chan string, 1),
		beam:     beamformer.New(),
		proc:     processor.New(),
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

	identifyBytes, _ := json.Marshal(map[string]string{
		"type":      "identify",
		"device_id": d.deviceID,
	})
	if err := conn.WriteMessage(websocket.TextMessage, identifyBytes); err != nil {
		return err
	}
	log.Printf("[data] Identified as %s", d.deviceID)

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
		default:
			log.Printf("[data] Unknown binary frame type: 0x%02x", data[0])
		}
	}
}

// streamMic subscribes to the mic, runs the processing pipeline and VAD gate,
// streams binary frames to the controller.
func (d *DataClient) streamMic(conn *websocket.Conn, stopCh <-chan struct{}, lockMic bool) {
	if d.mic == nil {
		log.Println("[data] streamMic: no mic")
		return
	}

	// Clear micActive on exit regardless of why we stopped — StopMic, stopCh
	// signal, or ALSA stream death. Without this, a mic death leaves micActive=true
	// and StartMic silently refuses to restart.
	defer func() {
		d.micMu.Lock()
		d.micActive = false
		d.micMu.Unlock()
		log.Println("[data] streamMic: exited")
	}()

	if lockMic {
		d.beam.Lock()
	}
	// Always unlock from this goroutine — prevents data race with Process()
	// that would occur if StopMic called Unlock from the control goroutine.
	defer d.beam.Unlock()

	ch := d.mic.Subscribe()
	defer d.mic.Unsubscribe(ch)

	cfg := config.Get()

	speechCount  := 0
	silenceCount := 0
	active       := false
	everActive   := false // true once active has been true at least once this turn
	buf          := make([]byte, 0, vadOwwChunkBytes*4)
	var seqNum   uint16

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

			snap := cfg.Snapshot()
			threshold    := snap.VadThreshold
			speechNeeded := snap.VadSpeechMs / 32
			silenceMax   := snap.VadSilenceMs / 32
			if speechNeeded < 1 { speechNeeded = 1 }
			if silenceMax   < 1 { silenceMax   = 1 }

			beamEnabled := snap.BeamformingEnabled != nil && *snap.BeamformingEnabled
			beamAngle   := float64(-1)
			if snap.BeamAngle != nil {
				beamAngle = *snap.BeamAngle
			}
			mono, angle := d.beam.Process(raw, beamAngle, beamEnabled)

			// ── Processing pipeline ──────────────────────────────────────
			// Detect speech on raw beamformed output (pre-AGC) so VAD
			// threshold is consistent regardless of AGC gain state.
			rms    := vadPeriodRMS(mono)
			speech := rms >= threshold

			// NS + AGC pipeline.
			// NS uses RNNoise to reduce background noise before AGC.
			// AGC release frozen during silence to prevent noise amplification.
			mono = d.proc.Process(mono, true, speech)
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

			if speech {
				silenceCount = 0
				if !active {
					speechCount++
					if speechCount >= speechNeeded {
						active = true
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
			}
		}
	}
}
