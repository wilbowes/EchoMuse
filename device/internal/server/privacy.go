package server

import (
	"log"
	"time"

	internalLed "github.com/wilbowes/EchoMuse/internal/bindings/led"
)

// Keeping Amazon's privacy driver in step with our mute, on kernels that have
// it (internal/bindings/led/privacy.go). Both toggle on every press, so they
// agree until one changes without the other, which a reboot while muted does:
// we restore muted, the driver boots unmuted, and from then on each press
// leaves them opposite — including a live mic under a lit mute button. Found
// on 15LE 2026-09-26.

type privacyAction int

const (
	privacyInSync privacyAction = iota
	// We are muted and the driver is not: tell it. Software can do this.
	privacyEnterDriver
	// The driver is muted and we are not. Software cannot take the driver
	// out, so mute ourselves: both then say muted and the mic is off, and
	// the next press unmutes both.
	privacyMuteOurs
	// The GPIO latch was pressed out of mute: follow it.
	privacyUnmuteOurs
)

// decideLatch is decidePrivacy until the GPIO latch has once agreed with us.
// Before that, an unmuted latch under our mute is a restore it has not taken
// (or a failed entry), not a press, and must not open the mic.
func decideLatch(ours, latch, trusted bool) privacyAction {
	if trusted && ours && !latch {
		return privacyUnmuteOurs
	}
	return decidePrivacy(ours, latch)
}

func decidePrivacy(ours, driver bool) privacyAction {
	switch {
	case ours && !driver:
		return privacyEnterDriver
	case !ours && driver:
		return privacyMuteOurs
	}
	return privacyInSync
}

// privacySettle outlasts the driver's 300ms entry delay.
const privacySettle = 700 * time.Millisecond

// reconcilePrivacySoon checks the two agree once a press has settled.
func (m *muteController) reconcilePrivacySoon() {
	// The GPIO latch is followed by monitorPrivacyLatch, which knows a press
	// from an entry that has not taken; this check would re-enter the latch
	// under an unmute press whose event is still debouncing.
	if !internalLed.PrivacyDriver() || internalLed.GPIOPrivacyDriver() {
		return
	}
	time.AfterFunc(privacySettle, func() { m.reconcilePrivacy(5) })
}

func (m *muteController) reconcilePrivacy(retries int) {
	if entering, err := internalLed.PrivacyEntering(); err == nil && entering && retries > 0 {
		time.AfterFunc(privacySettle, func() { m.reconcilePrivacy(retries - 1) })
		return
	}
	driver, err := internalLed.PrivacyMuted()
	if err != nil {
		log.Printf("Mute: %v", err)
		return
	}
	switch decidePrivacy(m.IsMuted(), driver) {
	case privacyEnterDriver:
		log.Println("Mute: privacy driver was unmuted while we are muted — muting it")
		if err := internalLed.EnterPrivacy(); err != nil {
			log.Printf("Mute: %v", err)
		}
	case privacyMuteOurs:
		log.Println("Mute: privacy driver is muted and cannot be unmuted from software — muting to match")
		if !m.IsMuted() {
			m.Toggle()
		}
	}
}

// GPIO events may be duplicated or lost, including while the daemon restarts.
// The physical latch is authoritative for each event and periodic check.
func (m *muteController) syncPrivacyLatch() {
	m.privacyMu.Lock()
	defer m.privacyMu.Unlock()
	latch, err := internalLed.PrivacyMuted()
	action := decideLatch(m.IsMuted(), latch, m.latchTrusted)
	if err == nil && action == privacyEnterDriver {
		err = internalLed.EnterPrivacy() // the next check verifies it took
	}
	if err != nil {
		if !m.latchFailing { // once per failure streak, not every second
			log.Printf("Mute: %v", err)
		}
		m.latchFailing = true
		return
	}
	m.latchFailing = false
	if action != privacyEnterDriver {
		m.latchTrusted = true
	}
	if action == privacyMuteOurs || action == privacyUnmuteOurs {
		m.Toggle()
	}
}

func (m *muteController) monitorPrivacyLatch() {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for ; ; <-ticker.C {
		m.syncPrivacyLatch()
	}
}
