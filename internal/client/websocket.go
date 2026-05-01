package client

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"strings"
	"time"

	"github.com/wilbowes/EchoMuse/internal/discovery"
	"github.com/wilbowes/EchoMuse/pkg/buttons"
	"golang.org/x/net/websocket"
)

const deviceID = "echo-dot"

// ButtonCallback is called when the server should be notified of a button event.
// The client registers this with the evdev controller.

type Message struct {
	Type      string          `json:"type"`
	DeviceID  string          `json:"device_id,omitempty"`
	ClickType int             `json:"clickType,omitempty"`
	Down      bool            `json:"down,omitempty"`
	LEDs      json.RawMessage `json:"leds,omitempty"`
}

// LEDCallback is called when the server sends LED commands.
type LEDCallback func(leds json.RawMessage)

type Client struct {
	deviceID    string
	ip          string
	ledCallback LEDCallback
	ws          *websocket.Conn
}

func NewClient(ledCallback LEDCallback) *Client {
	// Get our own LAN IP
	ip := getLocalIP()
	return &Client{
		deviceID:    deviceID,
		ip:          ip,
		ledCallback: ledCallback,
	}
}

// Run discovers the server via mDNS and maintains a persistent connection.
// Reconnects automatically on drop. Blocks until ctx is cancelled.
func (c *Client) Run(ctx context.Context) error {
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}

		// Discover server
		server, err := discovery.FindServer(ctx)
		if err != nil {
			return err
		}

		log.Printf("Connecting to Clara server at %s", server.Addr)
		if err := c.connect(ctx, server.Addr); err != nil {
			log.Printf("Connection lost: %v — reconnecting...", err)
			time.Sleep(5 * time.Second)
			continue
		}
	}
}

func (c *Client) connect(ctx context.Context, addr string) error {
	url    := fmt.Sprintf("ws://%s/device", addr)
	origin := fmt.Sprintf("http://%s/", c.ip)

	ws, err := websocket.Dial(url, "", origin)
	if err != nil {
		return fmt.Errorf("dial failed: %w", err)
	}
	defer ws.Close()
	c.ws = ws

	// Register
	reg := Message{
		Type:     "register",
		DeviceID: c.deviceID,
	}
	// Build registration with ip and capabilities inline
	regFull := map[string]interface{}{
		"type":         "register",
		"device_id":    c.deviceID,
		"ip":           c.ip,
		"capabilities": []string{"mic", "speaker", "leds", "buttons"},
	}
	regBytes, _ := json.Marshal(regFull)
	_ = reg
	if err := websocket.Message.Send(ws, string(regBytes)); err != nil {
		return fmt.Errorf("registration failed: %w", err)
	}

	// Wait for ack
	var ackRaw string
	ws.SetDeadline(time.Now().Add(10 * time.Second))
	if err := websocket.Message.Receive(ws, &ackRaw); err != nil {
		return fmt.Errorf("ack timeout: %w", err)
	}
	ws.SetDeadline(time.Time{}) // clear deadline

	var ack Message
	if err := json.Unmarshal([]byte(ackRaw), &ack); err != nil || ack.Type != "ack" {
		return fmt.Errorf("unexpected ack: %s", ackRaw)
	}
	log.Printf("Registered with Clara server as %s", c.deviceID)

	// Read loop
	done := make(chan error, 1)
	go func() {
		for {
			var raw string
			if err := websocket.Message.Receive(ws, &raw); err != nil {
				done <- err
				return
			}

			var msg Message
			if err := json.Unmarshal([]byte(raw), &msg); err != nil {
				continue
			}

			switch msg.Type {
			case "leds":
				if c.ledCallback != nil && msg.LEDs != nil {
					c.ledCallback(msg.LEDs)
				}
			case "ping":
				pong := `{"type":"pong"}`
				websocket.Message.Send(ws, pong)
			}
		}
	}()

	// Wait for context cancellation or connection drop
	select {
	case <-ctx.Done():
		return ctx.Err()
	case err := <-done:
		return err
	}
}

// SendButton sends a button event to the server.
func (c *Client) SendButton(event buttons.ButtonClickEvent) {
	if c.ws == nil {
		return
	}
	msg := map[string]interface{}{
		"type":      "button",
		"clickType": int(event.ClickType),
		"down":      event.Down,
		"button": map[string]string{
			"type": string(event.Button.Type),
		},
	}
	b, _ := json.Marshal(msg)
	websocket.Message.Send(c.ws, string(b))
}

func getLocalIP() string {
	conn, err := net.Dial("udp", "8.8.8.8:80")
	if err != nil {
		return "127.0.0.1"
	}
	defer conn.Close()
	addr := conn.LocalAddr().(*net.UDPAddr)
	ip := addr.IP.String()
	// Strip any zone identifier
	if idx := strings.IndexByte(ip, '%'); idx >= 0 {
		ip = ip[:idx]
	}
	return ip
}
