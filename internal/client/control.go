package client

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/wilbowes/EchoMuse/internal/discovery"
	"github.com/wilbowes/EchoMuse/pkg/buttons"
	"github.com/wilbowes/EchoMuse/pkg/led"
)

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

// ─── ControlClient ────────────────────────────────────────────────────────────

type ControlClient struct {
	deviceID string
	ip       string

	ledCallback         LEDCallback
	micStartCallback    MicStartCallback
	micStopCallback     MicStopCallback
	disconnectedCallback StateCallback
	connectedCallback    StateCallback

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

// OnDisconnected sets a callback invoked when the control connection is lost or not yet established.
func (c *ControlClient) OnDisconnected(cb StateCallback) {
	c.disconnectedCallback = cb
}

// OnConnected sets a callback invoked when the control connection is established and registered.
func (c *ControlClient) OnConnected(cb StateCallback) {
	c.connectedCallback = cb
}

// Run discovers the server via mDNS, notifies the data client after successful
// registration, and maintains a persistent control connection.
// Blocks until ctx is cancelled.
func (c *ControlClient) Run(ctx context.Context, data *DataClient) error {
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		server, err := discovery.FindServer(ctx)
		if err != nil {
			return err
		}

		// dataCtx is cancelled when control drops — data connection tears down too
		dataCtx, cancelData := context.WithCancel(ctx)

		// Start data client run loop — it blocks on readyCh until we call NotifyReady
		go func() {
			if err := data.Run(dataCtx); err != nil && err != context.Canceled {
				log.Printf("[data] stopped: %v", err)
			}
		}()

		// Signal disconnected state before attempting connection
		if c.disconnectedCallback != nil {
			c.disconnectedCallback()
		}

		log.Printf("[control] Connecting to %s", server.Addr)
		if err := c.connect(ctx, server.Addr, data); err != nil {
			log.Printf("[control] Connection lost: %v — reconnecting in 5s", err)
		}

		// Control dropped — signal disconnected, cancel data, wait before retrying
		if c.disconnectedCallback != nil {
			c.disconnectedCallback()
		}
		cancelData()
		time.Sleep(5 * time.Second)
	}
}

func (c *ControlClient) connect(ctx context.Context, addr string, data *DataClient) error {
	dialer := websocket.Dialer{
		HandshakeTimeout: 10 * time.Second,
	}
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

	// Register
	regBytes, _ := json.Marshal(map[string]interface{}{
		"type":         "register",
		"device_id":    c.deviceID,
		"ip":           c.ip,
		"capabilities": []string{"mic", "speaker", "leds", "buttons"},
	})
	if err := conn.WriteMessage(websocket.TextMessage, regBytes); err != nil {
		return err
	}

	// Wait for ack
	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	var ack controlMessage
	if err := conn.ReadJSON(&ack); err != nil {
		return err
	}
	conn.SetReadDeadline(time.Time{})
	if ack.Type != "ack" {
		return fmt.Errorf("unexpected ack type: %s", ack.Type)
	}
	log.Printf("[control] Registered as %s", c.deviceID)

	// Registration confirmed — signal connected state and unblock data client
	if c.connectedCallback != nil {
		c.connectedCallback()
	}
	data.NotifyReady(addr)

	// Ping loop
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			if err := c.writeJSON(map[string]string{"type": "ping"}); err != nil {
				return
			}
		}
	}()

	// Read loop — only reader on this connection
	for {
		var msg controlMessage
		if err := conn.ReadJSON(&msg); err != nil {
			return err
		}

		switch msg.Type {
		case "leds":
			if c.ledCallback != nil && msg.LEDs != nil {
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
		case "ping":
			c.writeJSON(map[string]string{"type": "pong"})
		case "pong":
			// response to our ping
		default:
			log.Printf("[control] Unknown message type: %s", msg.Type)
		}
	}
}

// SendButton sends a button event to the server. Safe for concurrent use.
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

// writeJSON marshals and sends a JSON message. Safe for concurrent use.
func (c *ControlClient) writeJSON(v interface{}) error {
	c.connMu.Lock()
	defer c.connMu.Unlock()
	if c.conn == nil {
		return nil
	}
	return c.conn.WriteJSON(v)
}

// getLocalIP returns the device's LAN IP address.
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
