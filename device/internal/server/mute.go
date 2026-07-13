package server

import (
	"log"
	"os/exec"
	"sync"

	internalLed "github.com/wilbowes/EchoMuse/internal/bindings/led"
	"github.com/wilbowes/EchoMuse/pkg/led"
)

type muteController struct {
	mu      sync.Mutex
	muted   bool
	ledCtrl func() led.Controller
	// dotMuted is set externally to block dot button events while muted
	onMuteChange func(muted bool)
}

func newMuteController(ledGetter func() led.Controller, onMuteChange func(muted bool)) *muteController {
	return &muteController{
		ledCtrl:      ledGetter,
		onMuteChange: onMuteChange,
	}
}

// SetOnMuteChange wires a callback invoked when mute state changes.
// B7 fix (2026-07-05 review): previously Server.SetMuteChangeCallback
// reached directly into m.mu/m.onMuteChange from outside this struct.
// Encapsulating the lock here keeps muteController responsible for its
// own synchronisation, matching every other muteController method.
func (m *muteController) SetOnMuteChange(cb func(muted bool)) {
	m.mu.Lock()
	m.onMuteChange = cb
	m.mu.Unlock()
}

func (m *muteController) IsMuted() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.muted
}

func (m *muteController) Toggle() {
	m.mu.Lock()
	m.muted = !m.muted
	muted := m.muted
	// Copy under the lock — SetOnMuteChange writes this field under mu from
	// the main goroutine, and button events can fire before that wiring
	// completes (SubscribeToButton starts the evdev goroutines first).
	cb := m.onMuteChange
	m.mu.Unlock()

	if muted {
		m.applyMute()
	} else {
		m.applyUnmute()
	}

	if cb != nil {
		cb(muted)
	}
}

// adcMuteCtls are the per-chip ADC mute control pairs, all four codecs
// (A: ch0/ch1 … D: ch6 + unused). C5 hardware fix (2026-07-07): only chip
// A (105/106) was muted before, leaving chips B–D — including ch6, the mic
// wake word and STT actually use — physically hot; the mic stream-stop was
// what made mute effective. Sibling controls confirmed from the full
// `tinymix -D 0` dump in device/tools/tinymix_controls_output.txt
// (captured 2026-07-06).
var adcMuteCtls = []string{
	"105", "106", // ADC_A
	"123", "124", // ADC_B
	"141", "142", // ADC_C
	"159", "160", // ADC_D
}

func setAdcMute(val string) {
	for _, ctl := range adcMuteCtls {
		exec.Command("tinymix", "-D", "0", ctl, val).Run()
	}
}

func (m *muteController) applyMute() {
	log.Println("Mute: mic muted")
	setAdcMute("1")
	m.showMuteLEDs()
	setMuteButtonLED(true)
}

func (m *muteController) applyUnmute() {
	log.Println("Mute: mic unmuted")
	setAdcMute("0")
	m.clearLEDs()
	setMuteButtonLED(false)
}

// setMuteButtonLED drives the discrete red LED under the mic-off button —
// stock-Alexa parity: the button itself shows muted, not just the ring.
// GPIO-backed and independent of the ring driver, so it needs no repaint
// protection (ring repaints can't stomp it) and survives every LED-mode
// transition for free. Direct binding call, same precedent as setAdcMute's
// tinymix exec above.
func setMuteButtonLED(on bool) {
	if err := internalLed.SetMuteButtonLED(on); err != nil {
		log.Printf("Mute button LED: %v", err)
	}
}

func (m *muteController) showMuteLEDs() {
	lc := m.ledCtrl()
	if lc == nil {
		return
	}
	leds := make([]led.Led, numLEDs)
	for i := 0; i < numLEDs; i++ {
		leds[i] = led.Led{ID: i, R: 180, G: 0, B: 0} // red ring
	}
	if err := lc.SetLEDs(leds...); err != nil {
		log.Printf("Mute LED set failed: %v", err)
	}
}

func (m *muteController) clearLEDs() {
	lc := m.ledCtrl()
	if lc == nil {
		return
	}
	clearLeds(lc)
}
