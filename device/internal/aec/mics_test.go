package aec

import (
	"bytes"
	"math"
	"path/filepath"
	"testing"
)

// micEcho is the playback as one microphone hears it: every microphone has
// its own delay, level and polarity from the speaker, which is the whole
// reason one filter cannot serve them all. Measured on a Dot 2 the paths peak
// 30-33 samples in and differ enough that a filter learnt on one removed
// between 23dB and MINUS 7dB on another (#814).
func micEcho(signal []int16, ch int) []int16 {
	delay := 24 + 5*ch
	gain := []float64{-0.9, 0.5, -0.6, 0.8, -0.4, 0.7, -0.55}[ch]
	out := make([]int16, len(signal))
	for i := delay + 7 + ch; i < len(signal); i++ {
		// A second tap, so the paths differ in shape and not only in scale.
		out[i] = int16(gain*float64(signal[i-delay]) + 0.3*gain*float64(signal[i-delay-7-ch]))
	}
	return out
}

// runMics plays frames [from, to) of signal to all seven microphones with
// `active` in use, and returns the cancellation on the active one in dB.
func runMics(c *Canceller, signal []int16, echoes [][]int16, active, from, to int) float64 {
	var in, out float64
	for f := from; f < to; f++ {
		lo, hi := f*FrameSize, (f+1)*FrameSize
		mics := make([][]byte, NumMics)
		for ch := range mics {
			mics[ch] = toBytes(echoes[ch][lo:hi])
		}
		res := c.ProcessMicsWithRef(mics, active, toBytes(signal[lo:hi]))
		in += rms(mics[active])
		out += rms(res)
	}
	return 20 * math.Log10(in/out)
}

func sevenEchoes(signal []int16) [][]int16 {
	echoes := make([][]int16, NumMics)
	for ch := range echoes {
		echoes[ch] = micEcho(signal, ch)
	}
	return echoes
}

// The fault in #814: a turn locks to a different microphone from the last
// one, and the wake sound plays in the first quarter second. Every
// microphone must already be converged when the pipeline moves to it. The
// single-filter control is the old behaviour, kept in the test so it cannot
// pass by the scenario being too easy.
func TestAMicrophoneChangeStaysCancelled(t *testing.T) {
	const learn, after = 150, 8 // 8 frames = 256ms: the wake sound
	signal := synth((learn + after) * FrameSize)
	echoes := sevenEchoes(signal)

	c := hwCanceller(64)
	runMics(c, signal, echoes, 6, 0, learn)
	got := runMics(c, signal, echoes, 3, learn, learn+after)

	one := hwCanceller(64)
	for f := 0; f < learn; f++ {
		lo, hi := f*FrameSize, (f+1)*FrameSize
		one.ProcessWithRef(toBytes(echoes[6][lo:hi]), toBytes(signal[lo:hi]))
	}
	var in, out float64
	for f := learn; f < learn+after; f++ {
		lo, hi := f*FrameSize, (f+1)*FrameSize
		mb := toBytes(echoes[3][lo:hi])
		in += rms(mb)
		out += rms(one.ProcessWithRef(mb, toBytes(signal[lo:hi])))
	}
	single := 20 * math.Log10(in/out)

	t.Logf("first %dms on a new microphone: %.1fdB per microphone, %.1fdB with one filter", after*32, got, single)
	if got < 20 {
		t.Fatalf("a microphone the pipeline moved to cancelled %.1fdB in its first frames, want 20dB", got)
	}
	if single > 6 {
		t.Fatalf("one filter cancelled %.1fdB on another microphone: the echo paths here are too alike to test anything", single)
	}
}

// Every microphone is as good as the one it would have been alone.
func TestEveryMicrophoneConverges(t *testing.T) {
	const frames = 150
	signal := synth(frames * FrameSize)
	echoes := sevenEchoes(signal)
	c := hwCanceller(64)
	runMics(c, signal, echoes, 6, 0, frames-20)
	// The states are independent, so the tail can be measured one
	// microphone at a time over the same frames only by rewinding: export
	// here, and import before each.
	saved, err := c.exportMicsForTest()
	if err != nil {
		t.Fatal(err)
	}
	for ch := 0; ch < NumMics; ch++ {
		if err := c.importMicsForTest(saved); err != nil {
			t.Fatal(err)
		}
		if att := runMics(c, signal, echoes, ch, frames-20, frames); att < 20 {
			t.Errorf("ch%d cancels %.1fdB once converged, want 20dB", ch, att)
		}
	}
}

// The software tap has one ring, drained once per period: it keeps one
// filter, on the microphone in use.
func TestSoftwareTapCancelsOnlyTheActiveMicrophone(t *testing.T) {
	c := New()
	c.SetParams(true, 0, 200)
	signal := synth(4 * FrameSize)
	echoes := sevenEchoes(signal)
	mics := make([][]byte, NumMics)
	for ch := range mics {
		mics[ch] = toBytes(echoes[ch][:FrameSize])
	}
	c.WriteFar(to48kStereo(signal[:FrameSize]))
	want := New()
	want.SetParams(true, 0, 200)
	want.WriteFar(to48kStereo(signal[:FrameSize]))
	if got := c.ProcessMicsWithRef(mics, 2, nil); !bytes.Equal(got, want.Process(mics[2])) {
		t.Fatal("off the hardware reference, ProcessMicsWithRef is not Process on the active microphone")
	}
	if c.hwFrames != 0 {
		t.Fatal("the per-microphone path ran without a hardware reference")
	}
}

// Anything the per-microphone path cannot take falls back to the single
// filter on the active microphone rather than returning nothing.
func TestMalformedMicrophoneSetFallsBack(t *testing.T) {
	c := hwCanceller(64)
	signal := synth(FrameSize)
	mic, ref := toBytes(micEcho(signal, 0)), toBytes(signal)
	six := make([][]byte, NumMics-1)
	for i := range six {
		six[i] = mic
	}
	if out := c.ProcessMicsWithRef(six, 0, ref); len(out) != len(mic) {
		t.Fatalf("six microphones returned %d bytes, want the active microphone's %d", len(out), len(mic))
	}
	seven := append(six, mic[:len(mic)-2])
	if out := c.ProcessMicsWithRef(seven, 0, ref); len(out) != len(mic) {
		t.Fatalf("a short microphone returned %d bytes, want %d", len(out), len(mic))
	}
	if out := c.ProcessMicsWithRef(seven, NumMics, ref); out != nil {
		t.Fatal("an active microphone outside the set returned audio")
	}
}

// All seven paths survive a restart, and a microphone the pipeline moves to
// after it is cancelled from its first frames.
func TestMicrophoneEchoPathsAreSavedAndLoaded(t *testing.T) {
	path := filepath.Join(t.TempDir(), "echo.bin")
	const learn = 150
	signal := synth((learn + 8) * FrameSize)
	echoes := sevenEchoes(signal)

	c := hwCanceller(64)
	c.SetStatePath(path)
	runMics(c, signal, echoes, 6, 0, learn)
	b, err := c.exportMicsForTest()
	if err != nil {
		t.Fatal(err)
	}
	writeState(c.micsPath(), b, 20)

	fresh := New()
	fresh.SetStatePath(path)
	fresh.SetParams(true, 0, 64)
	fresh.SetHardwareRef(true)
	if att := runMics(fresh, signal, echoes, 3, learn, learn+8); att < 20 {
		t.Fatalf("after a restart a microphone cancelled %.1fdB in its first frames, want 20dB", att)
	}
}

func TestMicrophoneEchoPathsRefuseWhatDoesNotFit(t *testing.T) {
	c := hwCanceller(64)
	good, err := c.exportMicsForTest()
	if err != nil {
		t.Fatal(err)
	}
	single, _ := c.ExportState()
	nan := append([]byte(nil), good...)
	copy(nan[len(nan)-4:], []byte{0, 0, 0xc0, 0x7f})
	count := append([]byte(nil), good...)
	count[len(micsMagic)] = NumMics - 1
	for name, b := range map[string][]byte{
		"empty":            nil,
		"a single path":    single,
		"truncated":        good[:len(good)-1],
		"trailing bytes":   append(append([]byte(nil), good...), 0),
		"six microphones":  count,
		"a non-finite tap": nan,
	} {
		if err := c.importMicsForTest(b); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
	long := hwCanceller(64)
	long.SetHardwareRef(false) // 200ms software-tap filter: no microphone states at all
	if err := long.importMicsForTest(good); err == nil {
		t.Error("a canceller off the hardware reference accepted per-microphone paths")
	}
	if err := c.importMicsForTest(good); err != nil {
		t.Errorf("its own export was refused: %v", err)
	}
}

// The states exist only with the hardware reference, and go when it does.
func TestMicrophoneStatesFollowTheHardwareReference(t *testing.T) {
	c := New()
	c.SetParams(true, 0, 200)
	if c.micStates[0] != nil {
		t.Fatal("per-microphone states built on the software tap")
	}
	c.SetHardwareRef(true)
	for ch, st := range c.micStates {
		if st == nil {
			t.Fatalf("ch%d has no state on the hardware reference", ch)
		}
	}
	c.SetHardwareRef(false)
	if c.micStates[0] != nil {
		t.Fatal("per-microphone states kept after leaving the hardware reference")
	}
	c.SetParams(false, 0, 200)
	c.SetHardwareRef(true)
	if c.micStates[0] != nil {
		t.Fatal("per-microphone states built while cancellation is off")
	}
}

// Nothing playing is most of the day. The six microphones not in use rest
// then, and a turn that starts on one of them after any length of quiet is
// still cancelled from its first frames: the wake sound is the first thing
// to play.
func TestUnusedMicrophonesRestWhileNothingPlays(t *testing.T) {
	const learn, idle, after = 150, 400, 8
	signal := synth((learn + idle + after) * FrameSize)
	room := synth((learn+idle+after)*FrameSize + 3)[3:] // near-end noise, unrelated to the playback
	for i := learn * FrameSize; i < (learn+idle)*FrameSize; i++ {
		signal[i] = 0 // bit-exact, as the loopback is when idle
	}
	echoes := sevenEchoes(signal)
	for ch := range echoes {
		for i := range echoes[ch] {
			echoes[ch][i] += room[i] / 64
		}
	}

	c := hwCanceller(64)
	runMics(c, signal, echoes, 6, 0, learn)
	if c.micFrames != learn*NumMics {
		t.Fatalf("while playing, %d cancellations ran over %d frames, want every microphone on each", c.micFrames, learn)
	}
	runMics(c, signal, echoes, 6, learn, learn+idle)
	// The echo's tail runs a few frames into the quiet, then quietHold.
	if ran, most := c.micFrames-learn*NumMics, uint64(idle+(quietHold+2)*(NumMics-1)); ran > most {
		t.Fatalf("%d cancellations ran over %d quiet frames, want no more than %d", ran, idle, most)
	}
	before := c.micFrames
	att := runMics(c, signal, echoes, 3, learn+idle, learn+idle+after)
	if c.micFrames-before != after*NumMics {
		t.Fatal("the microphones did not all resume when playback did")
	}

	always := hwCanceller(64)
	runMics(always, signal, echoes, 6, 0, learn)
	for f := learn; f < learn+idle; f++ { // the same quiet, one frame at a time, never resting
		always.farQuiet = 0
		runMics(always, signal, echoes, 6, f, f+1)
	}
	always.farQuiet = 0
	want := runMics(always, signal, echoes, 3, learn+idle, learn+idle+after)

	t.Logf("first %dms on a rested microphone: %.1fdB, against %.1fdB for one that never rested", after*32, att, want)
	if att < want-1.5 {
		t.Fatalf("a rested microphone cancelled %.1fdB in its first frames, %.1fdB for one that never rested", att, want)
	}
	if att < 12 {
		t.Fatalf("a rested microphone cancelled %.1fdB in its first frames, want 12dB over the room noise", att)
	}
}
