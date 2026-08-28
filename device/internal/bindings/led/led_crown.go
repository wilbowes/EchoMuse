//go:build crown

package led

import (
	"encoding/json"
	"log"
	"net"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

// crown has a screen, not an LED ring — no i2c frame device, no gpio444
// mute-button LED (both are biscuit-specific hardware, see i2c_controller.go
// and mute_button.go). The "leds" capability still stays on (CLAUDE.md: the
// controller's listening-ring hint drives a device's wake cue whether or not
// there is a physical ring to paint) — and now something actually renders
// it: crown_launcher's StatusOverlay draws a thin colour strip over
// whatever's on screen (browser, home screen, anything), the on-device
// answer to the screen-status-surface deferral this comment used to record.
//
// crownLedSocket names the Linux ABSTRACT-namespace socket StatusOverlay
// binds (StatusOverlay.SOCKET_NAME) — see
// device/crown_launcher/src/.../StatusOverlay.java. Same reasoning as
// crownPlaybackSocket in internal/bindings/speaker/socket_pcm_crown.go:
// fixed, not discovered (both sides ship from the same repo), abstract
// namespace (no stale socket file, no permission bits to reason about).
const crownLedSocket = "@com.echomuse.crownlauncher/led"

const ledWriteDeadline = 500 * time.Millisecond
const ledReconnectBackoff = 250 * time.Millisecond

// overlayController implements led.Controller by forwarding to the status
// strip instead of an i2c LED frame device. GetNumLEDs reports ONE: the
// strip is a single colour, not 12 addressable positions, so every call
// site that paints a 12-LED ring (clearLeds, direction arcs, scenes) works
// completely unmodified — SetLEDs just averages whatever it's handed into
// one colour rather than needing a crown-specific caller anywhere.
type overlayController struct {
	mu          sync.Mutex
	conn        net.Conn
	lastAttempt time.Time
}

// {"r":0,"g":144,"b":106} — deliberately not the framed binary protocol
// PlaybackServer uses. That exists to carry audio at realtime rate; this is
// one tiny message on state changes only (a handful of Hz at the busiest,
// during a pulse animation), so newline-delimited JSON costs nothing and
// needs no length-prefix framing on either side.
type overlayMsg struct {
	R uint8 `json:"r"`
	G uint8 `json:"g"`
	B uint8 `json:"b"`
}

func newOverlayController() *overlayController {
	return &overlayController{}
}

func (c *overlayController) ensureConn() net.Conn {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.conn != nil {
		return c.conn
	}
	if time.Since(c.lastAttempt) < ledReconnectBackoff {
		return nil
	}
	c.lastAttempt = time.Now()
	conn, err := net.DialTimeout("unix", crownLedSocket, ledWriteDeadline)
	if err != nil {
		return nil
	}
	log.Println("[led] status overlay socket connected")
	c.conn = conn
	return conn
}

func (c *overlayController) dropConn(reason error) {
	c.mu.Lock()
	if c.conn != nil {
		_ = c.conn.Close()
		c.conn = nil
	}
	c.mu.Unlock()
	log.Printf("[led] status overlay socket dropped: %v", reason)
}

func (*overlayController) Init() error { return nil }

// GetNumLEDs reports one — see the type comment.
func (*overlayController) GetNumLEDs() (int, error) { return 1, nil }

// SetLEDs averages every non-black entry it's given into one colour and
// sends it as the strip's colour. An all-black call (clearLeds, or any
// scene painting the ring off) sends {0,0,0}: StatusOverlay renders that
// as the strip HIDDEN, not a visible black bar sitting over the screen at
// idle.
func (c *overlayController) SetLEDs(leds ...led.Led) error {
	var rSum, gSum, bSum, n int
	for _, l := range leds {
		if l.R == 0 && l.G == 0 && l.B == 0 {
			continue
		}
		rSum += int(l.R)
		gSum += int(l.G)
		bSum += int(l.B)
		n++
	}
	var msg overlayMsg
	if n > 0 {
		msg = overlayMsg{R: uint8(rSum / n), G: uint8(gSum / n), B: uint8(bSum / n)}
	}

	conn := c.ensureConn()
	if conn == nil {
		// Same posture as socketPCM.Write: a dropped overlay update is
		// cosmetic — never worth failing the caller over. server.go logs
		// a SetLEDs error but nothing upstream treats it as fatal.
		return nil
	}
	_ = conn.SetWriteDeadline(time.Now().Add(ledWriteDeadline))
	b, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	b = append(b, '\n')
	if _, err := conn.Write(b); err != nil {
		c.dropConn(err)
		return nil
	}
	return nil
}

func NewDefaultController() (led.Controller, error) {
	return newOverlayController(), nil
}

// InitMuteButtonLED / SetMuteButtonLED: no discrete mute-button LED on this
// board. Mute is reflected in the status strip itself — SetLEDs paints it
// red the same way it paints biscuit's ring red — so there is nothing
// separate to drive here.
func InitMuteButtonLED() error       { return nil }
func SetMuteButtonLED(on bool) error { return nil }
