// Package jack reads the Echo Dot's 3.5mm headphone jack detect switch.
//
// The Dot carries a mediatek,mt8163-accdet block wired to the jack's detect
// contacts. Like the ambient light sensor, Android does not surface it: there
// is no headset state in any framework service on a debloated device. It is
// visible on the raw switch class, and on the ACCDET input node which reports
// nothing useful (EV=3, KEY=0).
//
// WHAT THE JACK DOES ON ITS OWN, measured on hardware 2026-08-09:
//
//   - Output routing is PHYSICAL. Inserting a plug diverts the signal through
//     the jack's own switch contacts. A voice response was heard in the
//     headphones while the mixer still read Headphone_Speaker_Mux=Speaker and
//     Ext_Headphone_Amp_Switch=Off — so those controls do NOT describe where
//     audio goes, and nothing here should try to drive them.
//   - accdet turns Ext_Speaker_Amp_Switch OFF on insert, which is right: it
//     mutes the internal speaker amp so the Dot is not also playing to the
//     room.
//   - Nothing turns it back ON when the plug is removed. That is the bug this
//     package exists for (issue #80). PcmSpeaker.Init is the only thing that
//     ever sets it, so before this the speaker stayed dead until the next
//     boot — which is why users reported that only a reboot restored it.
package jack

import (
	"context"
	"log"
	"os"
	"strconv"
	"strings"
	"time"
)

// statePath is the accdet switch's state file. 0 = nothing inserted,
// 1 = headset (with mic), 2 = headphone (no mic). A variable rather than a
// constant only so the tests can point it at a fixture.
var statePath = "/sys/class/switch/h2w/state"

// PollInterval bounds how quickly a plug change is noticed.
//
// Polled rather than driven off the ACCDET input node or a uevent socket: the
// input device reports no keys on this hardware (KEY=0), and a netlink uevent
// listener is a great deal of machinery for one boolean read of a sysfs file
// that costs a single open/read. A second of silence after unplugging is not
// worth a second transport.
const PollInterval = time.Second

// Inserted reports whether something is plugged into the jack.
//
// Absent hardware reads as NOT inserted rather than as an error, because every
// caller wants the same thing in that case: leave the speaker amp alone and
// behave exactly as a device with no jack always has.
func Inserted() bool {
	s, ok := State()
	return ok && s != 0
}

// State returns the raw accdet state, and whether it could be read at all.
//
// The distinction matters to Watch: a device where the file does not exist
// must not be read as a plug being pulled out, which would have us asserting
// the speaker amp on hardware we know nothing about.
func State() (int, bool) {
	b, err := os.ReadFile(statePath)
	if err != nil {
		return 0, false
	}
	n, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil {
		return 0, false
	}
	return n, true
}

// Present reports whether this device exposes a jack detect switch.
func Present() bool {
	_, ok := State()
	return ok
}

// Watch polls the jack and calls onChange for the state it finds, then again
// on every change.
//
// The first reading DOES fire, and dropping it is what broke boot-with-a-plug.
// It used to seed the baseline silently, on the reasoning that "Init has
// already put the speaker amp in the right state for the current jack
// position" — which was simply not true: PcmSpeaker.Init sets the amp ON
// unconditionally, because its
// click-free startup order needs the amp brought up onto a DAC already
// clocking silence, and it has no idea whether a plug is present.
//
// So a device booted with a cable in got the internal amp asserted by Init and
// nothing afterwards to correct it — accdet only acts on a transition, and a
// boot has no transition. Both outputs were wrong at once: the Dot played to
// the room, and the jack sat at minimum gain. Measured on hardware 2026-09-03
// (h2w=1, Ext_Speaker_Amp_Switch=On). Unplugging and replugging "fixed" it by
// manufacturing the edge the boot never had.
//
// onChange must therefore be idempotent, which SetJackRouting is.
//
// onChange runs on this goroutine. It shells out to tinymix, which takes
// milliseconds, so there is no handoff here; if it ever grows into anything
// slower it needs one, because a stalled callback freezes the baseline and
// every later transition is missed.
func Watch(ctx context.Context, onChange func(inserted bool)) {
	first, ok := State()
	if !ok {
		log.Printf("[jack] no detect switch at %s — headphone detection unavailable", statePath)
		return
	}
	log.Printf("[jack] detect switch at %s (state=%d)", statePath, first)

	was := first != 0
	onChange(was)
	t := time.NewTicker(PollInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			s, ok := State()
			if !ok {
				// A read that fails transiently is not a plug event. Hold the
				// baseline and try again rather than inventing a transition.
				continue
			}
			now := s != 0
			if now == was {
				continue
			}
			was = now
			log.Printf("[jack] %s (state=%d)", map[bool]string{true: "plug inserted", false: "plug removed"}[now], s)
			onChange(now)
		}
	}
}
