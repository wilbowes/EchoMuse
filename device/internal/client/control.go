package client

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os/exec"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/discovery"
	"github.com/wilbowes/EchoMuse/pkg/buttons"
	"github.com/wilbowes/EchoMuse/pkg/led"
)

// Version is set at build time via ldflags:
//
//	-ldflags "-X github.com/wilbowes/EchoMuse/internal/client.Version=v2.1.0"
var Version = "dev"

// ─── Message types ────────────────────────────────────────────────────────────

type controlMessage struct {
	Type      string          `json:"type"`
	DeviceID  string          `json:"device_id,omitempty"`
	ClickType int             `json:"clickType,omitempty"`
	Down      bool            `json:"down,omitempty"`
	LEDs      json.RawMessage `json:"leds,omitempty"`
}

// ─── Callbacks ────────────────────────────────────────────────────────────────

type LEDCallback func(leds []led.Led)
type MicStartCallback func()
type MicStopCallback func()
type StateCallback func()
type ConfigAppliedCallback func(msg config.ConfigMessage)

// ─── ControlClient ────────────────────────────────────────────────────────────

type ControlClient struct {
	deviceID string
	ip       string

	ledCallback           LEDCallback
	micStartCallback      MicStartCallback
	micStopCallback       MicStopCallback
	disconnectedCallback  StateCallback
	connectedCallback     StateCallback
	pendingCallback       StateCallback
	configAppliedCallback ConfigAppliedCallback

	conn   *websocket.Conn
	connMu sync.Mutex
}

func NewControlClient(
	deviceID string,
	ledCallback LEDCallback,
	micStartCallback MicStartCallback,
	micStopCallback MicStopCallback,
) *ControlClient {
	return &ControlClient{
		deviceID:         deviceID,
		ip:               getLocalIP(),
		ledCallback:      ledCallback,
		micStartCallback: micStartCallback,
		micStopCallback:  micStopCallback,
	}
}

func (c *ControlClient) OnDisconnected(cb StateCallback)             { c.disconnectedCallback = cb }
func (c *ControlClient) OnConnected(cb StateCallback)               { c.connectedCallback = cb }
func (c *ControlClient) OnPending(cb StateCallback)                 { c.pendingCallback = cb }
func (c *ControlClient) OnConfigApplied(cb ConfigAppliedCallback)   { c.configAppliedCallback = cb }

var errPending = fmt.Errorf("pending approval")

func (c *ControlClient) Run(ctx context.Context, data *DataClient) error {
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		// Show orange pulse while searching for server
		if c.disconnectedCallback != nil {
			c.disconnectedCallback()
		}

		server, err := discovery.FindServer(ctx)
		if err != nil {
			return err
		}

		dataCtx, cancelData := context.WithCancel(ctx)
		go func() {
			if err := data.Run(dataCtx); err != nil && err != context.Canceled {
				log.Printf("[data] stopped: %v", err)
			}
		}()


		log.Printf("[control] Connecting to %s", server.Addr)
		err = c.connect(ctx, server.Addr, data)

		cancelData()

		switch err {
		case errPending:
			log.Printf("[control] Device pending approval — retrying in 30s")
			if c.pendingCallback != nil {
				c.pendingCallback()
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(30 * time.Second):
			}
		default:
			if err != nil {
				log.Printf("[control] Connection lost: %v — reconnecting in 5s", err)
			}
			if c.disconnectedCallback != nil {
				c.disconnectedCallback()
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(5 * time.Second):
			}
		}
	}
}

func (c *ControlClient) connect(ctx context.Context, addr string, data *DataClient) error {
	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, _, err := dialer.DialContext(ctx, "ws://"+addr+"/control", http.Header{})
	if err != nil {
		return err
	}
	defer conn.Close()

	c.connMu.Lock()
	c.conn = conn
	c.connMu.Unlock()
	defer func() {
		c.connMu.Lock()
		c.conn = nil
		c.connMu.Unlock()
	}()

	regBytes, _ := json.Marshal(map[string]interface{}{
		"type":         "register",
		"device_id":    c.deviceID,
		"ip":           c.ip,
		"version":      Version,
		"capabilities": []string{"mic", "speaker", "leds", "buttons"},
	})
	if err := conn.WriteMessage(websocket.TextMessage, regBytes); err != nil {
		return err
	}

	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	var first controlMessage
	if err := conn.ReadJSON(&first); err != nil {
		return err
	}
	conn.SetReadDeadline(time.Time{})

	switch first.Type {
	case "pending":
		return errPending
	case "ack":
		// proceed
	default:
		return fmt.Errorf("unexpected first message: %s", first.Type)
	}

	log.Printf("[control] Registered as %s (version %s)", c.deviceID, Version)

	if c.connectedCallback != nil {
		c.connectedCallback()
	}
	data.NotifyReady(addr)

	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			if err := c.writeJSON(map[string]string{"type": "pong"}); err != nil {
				return
			}
		}
	}()

	for {
		var raw json.RawMessage
		if err := conn.ReadJSON(&raw); err != nil {
			return err
		}

		var peek struct {
			Type string `json:"type"`
		}
		if err := json.Unmarshal(raw, &peek); err != nil {
			continue
		}

		switch peek.Type {
		case "leds":
			var msg struct {
				LEDs json.RawMessage `json:"leds"`
			}
			if err := json.Unmarshal(raw, &msg); err == nil && c.ledCallback != nil {
				var leds []led.Led
				if err := json.Unmarshal(msg.LEDs, &leds); err == nil {
					c.ledCallback(leds)
				}
			}

		case "mic_start":
			if c.micStartCallback != nil {
				c.micStartCallback()
			}

		case "mic_stop":
			if c.micStopCallback != nil {
				c.micStopCallback()
			}

		case "config":
			var msg config.ConfigMessage
			if err := json.Unmarshal(raw, &msg); err == nil {
				cfg := config.Get()
				cfg.Apply(msg)
				log.Printf("[control] Config applied: vad_threshold=%.4f oww_threshold=%.2f",
					cfg.VadThreshold, cfg.OwwThreshold)
				if c.configAppliedCallback != nil {
					c.configAppliedCallback(msg)
				}
			}

		case "ping":
			c.writeJSON(map[string]string{"type": "pong"})

		case "pong":
			// ignore

		default:
			log.Printf("[control] Unknown message type: %s", peek.Type)
		}
	}
}

func (c *ControlClient) SendButton(event buttons.ButtonClickEvent) {
	log.Printf("[control] SendButton: clickType=%d down=%v", event.ClickType, event.Down)
	msg := map[string]interface{}{
		"type":      "button",
		"clickType": int(event.ClickType),
		"down":      event.Down,
		"button": map[string]string{
			"type": string(event.Button.Type),
		},
	}
	if err := c.writeJSON(msg); err != nil {
		log.Printf("[control] SendButton failed: %v", err)
	}
}

// SendLog sends a structured log entry to the controller.
// Safe for concurrent use — silently drops if not connected.
func (c *ControlClient) SendLog(level, message string) {
	_ = c.writeJSON(map[string]string{
		"type":    "log",
		"level":   level,
		"message": message,
	})
}

func (c *ControlClient) writeJSON(v interface{}) error {
	c.connMu.Lock()
	defer c.connMu.Unlock()
	if c.conn == nil {
		return nil
	}
	return c.conn.WriteJSON(v)
}

// GetSerialNo reads ro.serialno — stable device identifier matching adb devices output.
func GetSerialNo() string {
	out, err := exec.Command("getprop", "ro.serialno").Output()
	if err != nil {
		log.Printf("[control] Warning: could not read ro.serialno: %v", err)
		return "unknown-device"
	}
	serial := strings.TrimSpace(string(out))
	if serial == "" {
		return "unknown-device"
	}
	return serial
}

func getLocalIP() string {
	conn, err := net.Dial("udp", "8.8.8.8:80")
	if err != nil {
		return "127.0.0.1"
	}
	defer conn.Close()
	addr := conn.LocalAddr().(*net.UDPAddr)
	ip := addr.IP.String()
	if idx := strings.IndexByte(ip, '%'); idx >= 0 {
		ip = ip[:idx]
	}
	return ip
}
