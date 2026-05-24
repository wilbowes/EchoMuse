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
	"github.com/wilbowes/EchoMuse/pkg/mic"
	"github.com/wilbowes/EchoMuse/pkg/speaker"
)

// ─── Binary frame types ───────────────────────────────────────────────────────

const (
	frameTypeMic     = byte(0x01)
	frameTypeSpeaker = byte(0x02)
	frameTypeEOS     = byte(0x03)
	frameTypeVADEnd  = byte(0x04)
)

// ─── VAD constants ────────────────────────────────────────────────────────────

const (
	vadMicChannels   = 9
	vadByteSample    = 3
	vadFramePeriod   = 512
	vadBytePeriod    = vadFramePeriod * vadMicChannels * vadByteSample // 13824
	vadOwwChunkBytes = 1280 * 2                                        // 2560 bytes = 80ms
)


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

	beam            *beamformer.Beamformer
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

func (d *DataClient) StartMic() {
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
	go d.streamMic(conn, d.micStopCh)
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
	log.Println("[data] Mic streaming stopped")
}

func (d *DataClient) Run(ctx context.Context) error {
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case addr := <-d.readyCh:
			log.Printf("[data] Connecting to %s", addr)
			if err := d.connect(ctx, addr); err != nil && err != context.Canceled {
				log.Printf("[data] Connection lost: %v — waiting for control to reconnect", err)
			}
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

// streamMic subscribes to the mic, runs VAD gate, streams binary frames.
// VAD parameters are read from the shared config on each frame so that
// controller-pushed config changes take effect without a restart.
func (d *DataClient) streamMic(conn *websocket.Conn, stopCh <-chan struct{}) {
	if d.mic == nil {
		log.Println("[data] streamMic: no mic")
		return
	}

	ch := d.mic.Subscribe()
	defer d.mic.Unsubscribe(ch)

	// Snapshot config at stream start; the live config is re-read each
	// frame for threshold so changes are picked up within one frame window.
	cfg := config.Get()

	speechCount  := 0
	silenceCount := 0
	active       := false
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

	for {
		select {
		case <-stopCh:
			return
		case raw, ok := <-ch:
			if !ok {
				return
			}

			// Re-read live config each frame — picks up controller pushes
			snap := cfg.Snapshot()
			threshold    := snap.VadThreshold
			speechNeeded := snap.VadSpeechMs / 32
			silenceMax   := snap.VadSilenceMs / 32
			if speechNeeded < 1 { speechNeeded = 1 }
			if silenceMax   < 1 { silenceMax   = 1 }

			// Beamform: produce mono S16_LE + estimated source angle
			beamEnabled := snap.BeamformingEnabled != nil && *snap.BeamformingEnabled
			mono, angle := d.beam.Process(raw, snap.BeamAngle, beamEnabled)
			rms    := vadPeriodRMS(mono)
			speech := rms >= threshold

			// Notify direction listener — non-blocking, keep it fast
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
						// Signal end of speech to the controller
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
