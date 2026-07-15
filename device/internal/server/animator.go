package server

import (
	"log"
	"math"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

// AnimSpec describes a device-rendered ring animation, received from the
// controller as a led_anim control message. The device renders every frame
// locally on its own ticker, so animation smoothness no longer depends on
// controller event-loop scheduling or WiFi jitter. The controller remains
// the source of truth for *which* animation should be showing: it re-sends
// the current spec on reconnect, and TTLSec is the dead-man switch for the
// opposite failure (controller gone mid-animation).
type AnimSpec struct {
	// Pattern: "off", "solid", "spin" (head+trail dot), "rotate" (palette
	// rotates around the ring — the pride spinner), "pulse" (sinusoidal
	// throb), or "meter" (brightness follows the live speaker RMS — the
	// ring throbs with the response being played).
	Pattern string `json:"pattern"`
	// Colors semantics per pattern:
	//   solid        — palette painted 1:1 (1 colour = whole ring, else per-LED)
	//   spin         — [head, trail]
	//   rotate       — palette rotated one LED per frame
	//   pulse/meter  — palette whose brightness is modulated
	Colors [][3]uint8 `json:"colors"`
	// PeriodMs: frame interval for spin/rotate (0 → 80ms); full throb
	// cycle for pulse (0 → 2000ms). Meter ignores it (fixed 40ms tick).
	PeriodMs int `json:"periodMs"`
	// Listening marks solid frames as the listening ring so the
	// beamformer direction overlay engages (same flag as set_leds).
	Listening bool `json:"listening"`
	// TTLSec auto-clears the ring if no newer spec arrives — protects
	// against a controller that died mid-turn. 0 → no TTL.
	TTLSec int `json:"ttlSec"`
}

// animator owns the ring animation goroutine. One animation at a time; a
// new spec (or Stop) atomically replaces the current one via generation
// counting, so a stale goroutine can never paint over its successor.
type animator struct {
	mu  sync.Mutex
	gen int
}

const defaultAnimPeriod = 80 * time.Millisecond

// StartAnim replaces the current animation with spec.
func (s *Server) StartAnim(spec AnimSpec) {
	s.anim.mu.Lock()
	s.anim.gen++
	gen := s.anim.gen
	s.anim.mu.Unlock()

	switch spec.Pattern {
	case "off":
		s.SetLEDs(blackFrame(), boolPtr(false))
	case "solid":
		s.SetLEDs(paletteFrame(spec.Colors), boolPtr(spec.Listening))
		if spec.TTLSec > 0 {
			go s.animExpiry(gen, time.Duration(spec.TTLSec)*time.Second)
		}
	case "spin", "rotate":
		go s.runAnim(gen, spec)
	case "pulse":
		go s.runPulse(gen, spec)
	case "meter":
		go s.runMeter(gen, spec)
	default:
		log.Printf("StartAnim: unknown pattern %q — clearing ring", spec.Pattern)
		s.SetLEDs(blackFrame(), boolPtr(false))
	}
}

// StopAnim cancels any running animation without touching the ring — used
// when another subsystem (e.g. a controller set_leds frame) takes over.
func (s *Server) StopAnim() {
	s.anim.mu.Lock()
	s.anim.gen++
	s.anim.mu.Unlock()
}

// animCurrent reports whether gen is still the live animation.
func (s *Server) animCurrent(gen int) bool {
	s.anim.mu.Lock()
	defer s.anim.mu.Unlock()
	return gen == s.anim.gen
}

// animExpiry clears the ring when a TTL'd static frame outlives its
// dead-man window without being replaced.
func (s *Server) animExpiry(gen int, ttl time.Duration) {
	time.Sleep(ttl)
	if !s.animCurrent(gen) {
		return
	}
	log.Printf("led_anim: TTL expired with no replacement — clearing ring")
	s.SetLEDs(blackFrame(), boolPtr(false))
}

// runAnim renders spin/rotate frames until replaced or TTL-expired. Frames
// go through SetLEDs so the mute-ring and volume-arc paint suppressions
// (and baseLEDs recording for the hand-back repaint) apply unchanged.
func (s *Server) runAnim(gen int, spec AnimSpec) {
	period := defaultAnimPeriod
	if spec.PeriodMs > 0 {
		period = time.Duration(spec.PeriodMs) * time.Millisecond
	}
	var deadline time.Time
	if spec.TTLSec > 0 {
		deadline = time.Now().Add(time.Duration(spec.TTLSec) * time.Second)
	}

	ticker := time.NewTicker(period)
	defer ticker.Stop()

	pos := 0
	for {
		if !s.animCurrent(gen) {
			return
		}
		if !deadline.IsZero() && time.Now().After(deadline) {
			log.Printf("led_anim: TTL expired with no replacement — clearing ring")
			if s.animCurrent(gen) {
				s.SetLEDs(blackFrame(), boolPtr(false))
			}
			return
		}
		s.SetLEDs(animFrame(spec, pos), boolPtr(false))
		pos = (pos + 1) % 12
		<-ticker.C
	}
}

// SetAudioLevel records the live speaker RMS (0..1) — fed by the speaker's
// levelTap on the ALSA pump goroutine, read by the meter animation.
func (s *Server) SetAudioLevel(rms float64) {
	s.audioLevel.Store(math.Float64bits(rms))
}

func (s *Server) getAudioLevel() float64 {
	return math.Float64frombits(s.audioLevel.Load())
}

// runPulse throbs the palette sinusoidally between 15% and 100% brightness
// over PeriodMs (default 2s) until replaced or TTL-expired.
func (s *Server) runPulse(gen int, spec AnimSpec) {
	cycle := 2 * time.Second
	if spec.PeriodMs > 0 {
		cycle = time.Duration(spec.PeriodMs) * time.Millisecond
	}
	var deadline time.Time
	if spec.TTLSec > 0 {
		deadline = time.Now().Add(time.Duration(spec.TTLSec) * time.Second)
	}
	base := paletteFrame(spec.Colors)
	start := time.Now()
	ticker := time.NewTicker(40 * time.Millisecond)
	defer ticker.Stop()
	for {
		if !s.animCurrent(gen) {
			return
		}
		if !deadline.IsZero() && time.Now().After(deadline) {
			log.Printf("led_anim: TTL expired with no replacement — clearing ring")
			if s.animCurrent(gen) {
				s.SetLEDs(blackFrame(), boolPtr(false))
			}
			return
		}
		phase := float64(time.Since(start)) / float64(cycle)
		b := 0.15 + 0.85*(0.5-0.5*math.Cos(2*math.Pi*phase))
		s.SetLEDs(scaleFrame(base, b), boolPtr(false))
		<-ticker.C
	}
}

// runMeter throbs the palette with the live speaker level: fast attack,
// slow decay envelope over the RMS the speaker reports at its ALSA write —
// so the ring follows what is audible *now*, not the controller's send
// pace (~5.5s of device buffer sits between the two). A 15% floor keeps
// the ring visibly owned by the turn through inter-word silence.
func (s *Server) runMeter(gen int, spec AnimSpec) {
	var deadline time.Time
	if spec.TTLSec > 0 {
		deadline = time.Now().Add(time.Duration(spec.TTLSec) * time.Second)
	}
	base := paletteFrame(spec.Colors)
	env := 0.0
	ticker := time.NewTicker(40 * time.Millisecond)
	defer ticker.Stop()
	for {
		if !s.animCurrent(gen) {
			return
		}
		if !deadline.IsZero() && time.Now().After(deadline) {
			log.Printf("led_anim: TTL expired with no replacement — clearing ring")
			if s.animCurrent(gen) {
				s.SetLEDs(blackFrame(), boolPtr(false))
			}
			return
		}
		// Speech RMS at the speaker typically peaks ~0.2-0.4 full-scale;
		// sqrt lifts the quiet tail so consonants still register.
		level := math.Sqrt(math.Min(1, s.getAudioLevel()/0.35))
		if level > env {
			env += 0.6 * (level - env) // fast attack
		} else {
			env += 0.12 * (level - env) // slow decay
		}
		b := 0.15 + 0.85*env
		s.SetLEDs(scaleFrame(base, b), boolPtr(false))
		<-ticker.C
	}
}

// scaleFrame returns frame with every channel scaled by b (0..1).
func scaleFrame(frame []led.Led, b float64) []led.Led {
	out := make([]led.Led, len(frame))
	for i, l := range frame {
		out[i] = led.Led{
			ID: l.ID,
			R:  uint8(float64(l.R)*b + 0.5),
			G:  uint8(float64(l.G)*b + 0.5),
			B:  uint8(float64(l.B)*b + 0.5),
		}
	}
	return out
}

// animFrame renders one frame of a spin/rotate animation.
func animFrame(spec AnimSpec, pos int) []led.Led {
	frame := make([]led.Led, 12)
	for i := range frame {
		frame[i].ID = i
	}
	switch spec.Pattern {
	case "spin":
		var head, trail [3]uint8
		if len(spec.Colors) > 0 {
			head = spec.Colors[0]
		}
		if len(spec.Colors) > 1 {
			trail = spec.Colors[1]
		}
		frame[pos%12] = led.Led{ID: pos % 12, R: head[0], G: head[1], B: head[2]}
		p := (pos + 11) % 12
		frame[p] = led.Led{ID: p, R: trail[0], G: trail[1], B: trail[2]}
	case "rotate":
		n := len(spec.Colors)
		if n == 0 {
			return frame
		}
		for i := range frame {
			c := spec.Colors[((i-pos)%n+n)%n]
			frame[i].R, frame[i].G, frame[i].B = c[0], c[1], c[2]
		}
	}
	return frame
}

// paletteFrame maps a colour list onto the ring: one colour fills the
// whole ring, otherwise colours map per-LED (short lists leave the rest
// dark, matching set_leds partial-frame behaviour).
func paletteFrame(colors [][3]uint8) []led.Led {
	frame := make([]led.Led, 12)
	for i := range frame {
		frame[i].ID = i
		var c [3]uint8
		switch {
		case len(colors) == 1:
			c = colors[0]
		case i < len(colors):
			c = colors[i]
		}
		frame[i].R, frame[i].G, frame[i].B = c[0], c[1], c[2]
	}
	return frame
}

func blackFrame() []led.Led {
	frame := make([]led.Led, 12)
	for i := range frame {
		frame[i].ID = i
	}
	return frame
}

func boolPtr(b bool) *bool { return &b }
