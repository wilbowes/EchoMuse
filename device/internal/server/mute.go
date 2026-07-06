package server

import (
	"log"
	"os/exec"
	"sync"

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
	m.mu.Unlock()

	if muted {
		m.applyMute()
	} else {
		m.applyUnmute()
	}

	if m.onMuteChange != nil {
		m.onMuteChange(muted)
	}
}

func (m *muteController) applyMute() {
	log.Println("Mute: mic muted")
	exec.Command("tinymix", "-D", "0", "105", "1").Run()
	exec.Command("tinymix", "-D", "0", "106", "1").Run()
	m.showMuteLEDs()
}

func (m *muteController) applyUnmute() {
	log.Println("Mute: mic unmuted")
	exec.Command("tinymix", "-D", "0", "105", "0").Run()
	exec.Command("tinymix", "-D", "0", "106", "0").Run()
	m.clearLEDs()
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
