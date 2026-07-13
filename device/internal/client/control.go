package client

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
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
	LockMic   bool            `json:"lock_mic,omitempty"`
}

// ─── Callbacks ────────────────────────────────────────────────────────────────

// LEDCallback receives a ring frame plus the controller's optional
// listening hint: non-nil when the message carried "listening", telling
// the server explicitly whether this frame is the listening ring (which
// enables the direction overlay). Nil on frames from older controllers —
// the server falls back to its all-green heuristic.
type LEDCallback func(leds []led.Led, listening *bool)
type MicStartCallback func(lockMic bool)
type MicStopCallback func()
type StateCallback func()
type ConfigAppliedCallback func(msg config.ConfigMessage)
type VolumeSetCallback func(level int)
type BeamLockCallback func(lock bool)

// WifiChangeCallback receives a wifi_change request. It must return
// quickly (the executor runs in its own goroutine) — the control
// connection is about to drop when the network switches.
type WifiChangeCallback func(ssid, psk string)

// ─── ControlClient ────────────────────────────────────────────────────────────

type ControlClient struct {
	deviceID string

	ledCallback           LEDCallback
	micStartCallback      MicStartCallback
	micStopCallback       MicStopCallback
	disconnectedCallback  StateCallback
	connectedCallback     StateCallback
	pendingCallback       StateCallback
	configAppliedCallback ConfigAppliedCallback
	volumeSetCallback     VolumeSetCallback
	beamLockCallback      BeamLockCallback
	speakerFlushCallback  StateCallback
	wifiChangeCallback    WifiChangeCallback
	wifiCommitCallback    StateCallback
	wifiScanCallback      StateCallback

	conn         *websocket.Conn
	connMu       sync.Mutex

	// serverAddr is the controller address (host:port), set on successful connect.
	// Used by the shell dialler to connect back to the controller.
	serverAddr   string
	serverAddrMu sync.RWMutex

	// shellCancel cancels a running shell session when shell_close is received.
	shellCancel context.CancelFunc
	shellMu     sync.Mutex
}

func NewControlClient(
	deviceID string,
	ledCallback LEDCallback,
	micStartCallback MicStartCallback,
	micStopCallback MicStopCallback,
) *ControlClient {
	return &ControlClient{
		deviceID:         deviceID,
		ledCallback:      ledCallback,
		micStartCallback: micStartCallback,
		micStopCallback:  micStopCallback,
	}
}

func (c *ControlClient) OnDisconnected(cb StateCallback)           { c.disconnectedCallback = cb }
func (c *ControlClient) OnConnected(cb StateCallback)             { c.connectedCallback = cb }
func (c *ControlClient) OnPending(cb StateCallback)               { c.pendingCallback = cb }
func (c *ControlClient) OnConfigApplied(cb ConfigAppliedCallback) { c.configAppliedCallback = cb }
func (c *ControlClient) OnVolumeSet(cb VolumeSetCallback)         { c.volumeSetCallback = cb }
func (c *ControlClient) OnBeamLock(cb BeamLockCallback)           { c.beamLockCallback = cb }
func (c *ControlClient) OnSpeakerFlush(cb StateCallback)          { c.speakerFlushCallback = cb }
func (c *ControlClient) OnWifiChange(cb WifiChangeCallback)       { c.wifiChangeCallback = cb }
func (c *ControlClient) OnWifiCommit(cb StateCallback)            { c.wifiCommitCallback = cb }
func (c *ControlClient) OnWifiScan(cb StateCallback)              { c.wifiScanCallback = cb }

// IsConnected reports whether the control WebSocket is registered and
// live — the wifi change executor's "controller reachable" gate.
func (c *ControlClient) IsConnected() bool {
	c.connMu.Lock()
	defer c.connMu.Unlock()
	return c.conn != nil
}

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

		// Fast path: try the last-known controller address before mDNS.
		// Speeds up ordinary reconnects, and after a WiFi network change
		// it's what makes a controller on a different subnet reachable at
		// all (multicast rarely crosses subnets, so mDNS alone would fail
		// the change's reconnect gate and revert a working network).
		addr := c.lastKnownAddr()
		if addr != "" && probeTCP(addr, 3*time.Second) {
			log.Printf("[control] Last-known controller %s reachable — skipping mDNS", addr)
		} else {
			server, err := discovery.FindServer(ctx)
			if err != nil {
				return err
			}
			addr = server.Addr
		}

		dataCtx, cancelData := context.WithCancel(ctx)
		go func() {
			if err := data.Run(dataCtx); err != nil && err != context.Canceled {
				log.Printf("[data] stopped: %v", err)
			}
		}()

		log.Printf("[control] Connecting to %s", addr)
		err := c.connect(ctx, addr, data)

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

	// Store controller address for outbound shell connections
	c.serverAddrMu.Lock()
	c.serverAddr = addr
	c.serverAddrMu.Unlock()

	reg := map[string]interface{}{
		"type":         "register",
		"device_id":    c.deviceID,
		"version":      Version,
		"capabilities": []string{"mic", "speaker", "leds", "buttons"},
	}
	// Resolved fresh per registration: a cached-at-startup value goes stale
	// after a WiFi change, and if the process started while the network was
	// down (e.g. wifi.RecoverIfPending bouncing WiFi) it cached 127.0.0.1
	// forever. Omitted on failure so the controller falls back to the WS
	// peer address.
	if ip := getLocalIP(); ip != "127.0.0.1" {
		reg["ip"] = ip
	}
	regBytes, _ := json.Marshal(reg)
	// Send register BEFORE publishing conn — prevents concurrent SendButton /
	// SendMuteState from racing this write on the same gorilla conn.
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

	// Handshake complete — now safe to publish conn for concurrent use.
	// done is closed when this connection exits, stopping the pong ticker.
	done := make(chan struct{})
	defer close(done)

	c.connMu.Lock()
	c.conn = conn
	c.connMu.Unlock()
	defer func() {
		c.connMu.Lock()
		c.conn = nil
		c.connMu.Unlock()
	}()

	if c.connectedCallback != nil {
		c.connectedCallback()
	}
	data.NotifyReady(addr)

	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-ticker.C:
				if err := c.writeJSON(map[string]string{"type": "pong"}); err != nil {
					return
				}
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
				LEDs      json.RawMessage `json:"leds"`
				Listening *bool           `json:"listening"`
			}
			if err := json.Unmarshal(raw, &msg); err == nil && c.ledCallback != nil {
				var leds []led.Led
				if err := json.Unmarshal(msg.LEDs, &leds); err == nil {
					c.ledCallback(leds, msg.Listening)
				}
			}

		case "mic_start":
			if c.micStartCallback != nil {
				var msg controlMessage
				_ = json.Unmarshal(raw, &msg)
				c.micStartCallback(msg.LockMic)
			}

		case "mic_stop":
			if c.micStopCallback != nil {
				c.micStopCallback()
			}

		// beam_lock/beam_unlock: controller-driven beamformer control for the
		// continuous wake stream. Sent at wake detection (lock onto the
		// speaker's perimeter mic mid-utterance, no stream restart) and at
		// turn end (back to ch6 omni for wake listening).
		case "beam_lock":
			if c.beamLockCallback != nil {
				c.beamLockCallback(true)
			}

		case "beam_unlock":
			if c.beamLockCallback != nil {
				c.beamLockCallback(false)
			}

		case "volume_set":
			// Controller forwarding a volume command from HA (MediaPlayerCommandRequest).
			// level is an integer 0–175 matching the device's native tinymix range.
			var msg struct {
				Level int `json:"level"`
			}
			if err := json.Unmarshal(raw, &msg); err == nil && c.volumeSetCallback != nil {
				c.volumeSetCallback(msg.Level)
			}

		case "config":
			var msg config.ConfigMessage
			if err := json.Unmarshal(raw, &msg); err == nil {
				cfg := config.Get()
				cfg.Apply(msg)
				snap := cfg.Snapshot() // read back under the config lock
				log.Printf("[control] Config applied: vad_threshold=%.4f oww_threshold=%.2f",
					snap.VadThreshold, snap.OwwThreshold)
				if c.configAppliedCallback != nil {
					c.configAppliedCallback(msg)
				}
			}

		case "shell_open":
			// Controller is requesting a shell session.
			// Dial outbound to ws://controller/shell/{device_id} and pipe sh stdio.
			// pty:true (dashboard terminal) requests an interactive PTY session;
			// absent (programmatic sessions — OTA transfers, _shell_run) keeps
			// the plain pipe, whose unechoed, prompt-free output those callers
			// parse.
			var shellMsg struct {
				Pty bool `json:"pty"`
			}
			_ = json.Unmarshal(raw, &shellMsg)
			log.Printf("[control] shell_open received (pty=%v) — dialling controller shell endpoint", shellMsg.Pty)
			c.shellMu.Lock()
			if c.shellCancel != nil {
				// Close any existing session first
				c.shellCancel()
			}
			shellCtx, shellCancel := context.WithCancel(ctx)
			c.shellCancel = shellCancel
			c.shellMu.Unlock()

			c.serverAddrMu.RLock()
			controllerAddr := c.serverAddr
			c.serverAddrMu.RUnlock()

			go c.runShellSession(shellCtx, controllerAddr, shellMsg.Pty)

		case "shell_close":
			log.Printf("[control] shell_close received — closing shell session")
			c.shellMu.Lock()
			if c.shellCancel != nil {
				c.shellCancel()
				c.shellCancel = nil
			}
			c.shellMu.Unlock()

		case "wifi_change":
			// Safe network switch (see internal/wifi). The executor owns
			// the whole sequence device-side — this connection is about to
			// die when the network flips.
			var msg struct {
				SSID string `json:"ssid"`
				PSK  string `json:"psk"`
			}
			if err := json.Unmarshal(raw, &msg); err == nil && c.wifiChangeCallback != nil {
				log.Printf("[control] wifi_change received (ssid=%q)", msg.SSID)
				c.wifiChangeCallback(msg.SSID, msg.PSK)
			}

		case "wifi_commit":
			// Controller acknowledged a successful change — finalise it.
			if c.wifiCommitCallback != nil {
				c.wifiCommitCallback()
			}

		case "wifi_scan":
			if c.wifiScanCallback != nil {
				c.wifiScanCallback()
			}

		case "speaker_flush":
			// Barge-in: controller detected the wake word during TTS
			// playback and wants the buffered audio cut immediately.
			log.Printf("[control] speaker_flush received — discarding buffered playback")
			if c.speakerFlushCallback != nil {
				c.speakerFlushCallback()
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

// Shell input frame types (PTY sessions only). The dashboard sends framed
// binary messages; the controller proxies them verbatim. Plain-pipe
// sessions receive raw unframed bytes, as before.
const (
	shellFrameStdin  = 0x00 // payload: raw stdin bytes
	shellFrameResize = 0x01 // payload: cols uint16 BE, rows uint16 BE
)

// runShellSession dials the controller's /shell/{device_id} endpoint,
// spawns sh, and pipes its stdio bidirectionally until ctx is cancelled
// or the connection drops.
//
// pty=true attaches sh to a pseudo-terminal (interactive mksh: prompt,
// line editing, job control, SIGWINCH) and expects framed input; the
// ?pty=1 query tells the controller which mode was actually established
// so the dashboard can match its framing. pty=false is the legacy raw
// pipe used by programmatic sessions. If PTY allocation fails, the
// session falls back to the pipe so a shell is always available.
func (c *ControlClient) runShellSession(ctx context.Context, controllerAddr string, pty bool) {
	var master, slave *os.File
	if pty {
		var err error
		master, slave, err = openPty()
		if err != nil {
			log.Printf("[shell] PTY allocation failed (%v) — falling back to pipe", err)
			pty = false
		}
	}

	shellURL := "ws://" + controllerAddr + "/shell/" + c.deviceID
	if pty {
		shellURL += "?pty=1"
	}
	log.Printf("[shell] Connecting to controller: %s", shellURL)

	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, _, err := dialer.DialContext(ctx, shellURL, http.Header{})
	if err != nil {
		log.Printf("[shell] Failed to connect to controller: %v", err)
		if master != nil {
			master.Close()
			slave.Close()
		}
		return
	}
	defer conn.Close()

	log.Printf("[shell] Connected — spawning sh (pty=%v)", pty)

	cmd := exec.CommandContext(ctx, "/system/bin/sh")

	// output is the fd read for shell output; input the fd written for
	// stdin — the PTY master serves as both.
	var output io.Reader
	var input io.WriteCloser

	if pty {
		cmd.Stdin, cmd.Stdout, cmd.Stderr = slave, slave, slave
		cmd.Env = append(os.Environ(), "TERM=xterm-256color")
		// New session with the PTY slave (child fd 0) as controlling TTY —
		// this is what gives mksh an interactive terminal.
		cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true}
		output = master
		input = master
	} else {
		stdin, err := cmd.StdinPipe()
		if err != nil {
			log.Printf("[shell] StdinPipe: %v", err)
			return
		}
		stdout, err := cmd.StdoutPipe()
		if err != nil {
			log.Printf("[shell] StdoutPipe: %v", err)
			return
		}
		cmd.Stderr = cmd.Stdout // merge stderr into stdout
		output = stdout
		input = stdin
	}

	if err := cmd.Start(); err != nil {
		log.Printf("[shell] cmd.Start: %v", err)
		if master != nil {
			master.Close()
			slave.Close()
		}
		return
	}
	if pty {
		// Child holds its own slave fd now; keeping ours open would stop
		// the master from ever reading EOF after the shell exits.
		slave.Close()
	}

	done := make(chan struct{})

	// shell output → WebSocket
	go func() {
		defer close(done)
		buf := make([]byte, 4096)
		for {
			n, err := output.Read(buf)
			if n > 0 {
				if werr := conn.WriteMessage(websocket.BinaryMessage, buf[:n]); werr != nil {
					log.Printf("[shell] write to WS: %v", werr)
					break
				}
			}
			if err != nil {
				break
			}
		}
	}()

	// WebSocket → shell input (framed in PTY mode, raw in pipe mode)
	go func() {
		for {
			_, data, err := conn.ReadMessage()
			if err != nil {
				break
			}
			if !pty {
				if _, err := input.Write(data); err != nil {
					return
				}
				continue
			}
			if len(data) == 0 {
				continue
			}
			switch data[0] {
			case shellFrameStdin:
				if _, err := input.Write(data[1:]); err != nil {
					return
				}
			case shellFrameResize:
				if len(data) >= 5 {
					cols := binary.BigEndian.Uint16(data[1:3])
					rows := binary.BigEndian.Uint16(data[3:5])
					if err := setWinsize(master, cols, rows); err != nil {
						log.Printf("[shell] TIOCSWINSZ: %v", err)
					}
				}
			}
		}
		input.Close()
	}()

	// Wait for shell exit, ctx cancel, or connection drop
	select {
	case <-done:
	case <-ctx.Done():
	}

	cmd.Process.Kill()
	cmd.Wait()
	if master != nil {
		master.Close()
	}
	log.Println("[shell] Session closed")
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

// SendMuteState notifies the controller of the current mute state.
// Safe for concurrent use — silently drops if not connected.
func (c *ControlClient) SendMuteState(muted bool) {
	_ = c.writeJSON(map[string]interface{}{
		"type":  "mute_state",
		"muted": muted,
	})
}

// SendVolumeState notifies the controller of the current volume level (0–175).
// Called on connect (to sync controller state) and after every local change.
// Safe for concurrent use — silently drops if not connected.
func (c *ControlClient) SendVolumeState(level int) {
	_ = c.writeJSON(map[string]interface{}{
		"type":  "volume_state",
		"level": level,
	})
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

// SendWifiResult reports the outcome of a wifi_change attempt.
// Safe for concurrent use — silently drops if not connected.
func (c *ControlClient) SendWifiResult(ok bool, ssid, errMsg string) {
	_ = c.writeJSON(map[string]interface{}{
		"type":  "wifi_result",
		"ok":    ok,
		"ssid":  ssid,
		"error": errMsg,
	})
}

// SendBleAdverts forwards a batch of BLE advertisements to the controller
// (bluetooth_proxy path). adverts is marshalled as-is — []bluetooth.Advert,
// whose Data field JSON-encodes as base64. Safe for concurrent use —
// silently drops if not connected (adverts are ephemeral by nature).
func (c *ControlClient) SendBleAdverts(adverts interface{}) {
	_ = c.writeJSON(map[string]interface{}{
		"type":    "ble_adverts",
		"adverts": adverts,
	})
}

// SendWifiScanResult reports scan results (or a scan error) upstream.
// networks is marshalled as-is; pass nil with errMsg on failure.
func (c *ControlClient) SendWifiScanResult(networks interface{}, errMsg string) {
	_ = c.writeJSON(map[string]interface{}{
		"type":     "wifi_scan_result",
		"networks": networks,
		"error":    errMsg,
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

func (c *ControlClient) lastKnownAddr() string {
	c.serverAddrMu.RLock()
	defer c.serverAddrMu.RUnlock()
	return c.serverAddr
}

// probeTCP reports whether addr (host:port) accepts a TCP connection
// within timeout.
func probeTCP(addr string, timeout time.Duration) bool {
	conn, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return false
	}
	conn.Close()
	return true
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
