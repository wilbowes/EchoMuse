package client

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"log"
	"math"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/pkg/mic"
	"github.com/wilbowes/EchoMuse/pkg/speaker"
)

// ─── Binary frame types ───────────────────────────────────────────────────────

const (
	frameTypeMic     = byte(0x01) // device → server: mic PCM
	frameTypeSpeaker = byte(0x02) // server → device: speaker PCM
	frameTypeEOS     = byte(0x03) // server → device: end of audio stream
)

// ─── VAD constants ────────────────────────────────────────────────────────────

const (
	vadMicChannels   = 9
	vadByteSample    = 3
	vadFramePeriod   = 512
	vadBytePeriod    = vadFramePeriod * vadMicChannels * vadByteSample // 13824
	vadOwwChunkBytes = 1280 * 2                                        // 2560 bytes = 80ms
)

func vadCh() int {
	if v := os.Getenv("VAD_CHANNEL"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return 0
}

func vadThresh() float64 {
	if v := os.Getenv("VAD_THRESHOLD"); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return 0.004
}

func vadSpeechN() int {
	if v := os.Getenv("VAD_SPEECH_MS"); v != "" {
		if ms, err := strconv.Atoi(v); err == nil && ms > 0 {
			return ms / 32
		}
	}
	return 1
}

func vadSilenceN() int {
	if v := os.Getenv("VAD_SILENCE_MS"); v != "" {
		if ms, err := strconv.Atoi(v); err == nil && ms > 0 {
			return ms / 32
		}
	}
	return 19 // ~600ms
}

func vadExtractMono(raw []byte, channel int) []byte {
	frameSize := vadMicChannels * vadByteSample
	nFrames := len(raw) / frameSize
	offset := channel * vadByteSample
	out := make([]byte, nFrames*2)
	for i := 0; i < nFrames; i++ {
		base := i*frameSize + offset
		out[i*2] = raw[base+1]
		out[i*2+1] = raw[base+2]
	}
	return out
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

	// readyCh receives the server address from ControlClient after successful
	// registration. This is the synchronisation point — data never connects
	// before control has registered.
	readyCh chan string

	// micActive guards mic streaming lifecycle
	micMu     sync.Mutex
	micActive bool
	micStopCh chan struct{}

	// conn and write mutex for concurrent mic writes alongside speaker reads
	conn   *websocket.Conn
	connMu sync.Mutex
}

func NewDataClient(deviceID string, microphone mic.Subscribable, spk speaker.Speaker) *DataClient {
	return &DataClient{
		deviceID: deviceID,
		mic:      microphone,
		spk:      spk,
		readyCh:  make(chan string, 1),
	}
}

// NotifyReady is called by ControlClient after successful registration.
// It unblocks the data connection attempt with the confirmed server address.
func (d *DataClient) NotifyReady(serverAddr string) {
	select {
	case d.readyCh <- serverAddr:
	default:
		// Previous notification not yet consumed — replace it
		select {
		case <-d.readyCh:
		default:
		}
		d.readyCh <- serverAddr
	}
}

// StartMic signals the data client to begin streaming mic audio.
// Called by ControlClient when it receives mic_start from server.
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

// StopMic signals the mic streaming goroutine to stop.
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

// Run waits for control to signal readiness, then connects to the data plane.
// On disconnect, waits for control to signal again before reconnecting.
// Blocks until ctx is cancelled.
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
			// Don't reconnect independently — wait for control to signal again
		}
	}
}

func (d *DataClient) connect(ctx context.Context, addr string) error {
	dialer := websocket.Dialer{
		HandshakeTimeout: 10 * time.Second,
	}
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

	// Identify so server can associate with control connection
	identifyBytes, _ := json.Marshal(map[string]string{
		"type":      "identify",
		"device_id": d.deviceID,
	})
	if err := conn.WriteMessage(websocket.TextMessage, identifyBytes); err != nil {
		return err
	}
	log.Printf("[data] Identified as %s", d.deviceID)

	// Read loop — speaker frames arrive here
	// gorilla: safe to have one reader and one writer concurrently
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
// gorilla supports one concurrent reader and one concurrent writer — this
// is the writer; connect()'s read loop is the reader.
func (d *DataClient) streamMic(conn *websocket.Conn, stopCh <-chan struct{}) {
	if d.mic == nil {
		log.Println("[data] streamMic: no mic")
		return
	}

	ch := d.mic.Subscribe()
	defer d.mic.Unsubscribe(ch)

	channel      := vadCh()
	threshold    := vadThresh()
	speechNeeded := vadSpeechN()
	silenceMax   := vadSilenceN()

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

			mono   := vadExtractMono(raw, channel)
			rms    := vadPeriodRMS(mono)
			speech := rms >= threshold

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
