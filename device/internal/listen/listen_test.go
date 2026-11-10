package listen

import (
	"testing"
	"time"
)

var t0 = time.Unix(1000, 0)

func at(i int) time.Time { return t0.Add(time.Duration(i) * frameDur) }

func pcm(tag byte) []byte {
	b := make([]byte, FrameBytes)
	b[0], b[FrameBytes-1] = tag, tag
	return b
}

// tag reads a frame's index from its last byte, which the wake lead's
// silencing of the first frame never reaches.
func tag(f []byte) byte { return f[FrameBytes-1] }

// fill pushes frames 0..n-1 into a closed gate, tagging each with its index.
func fill(t *testing.T, g *Gate, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		if out, _, _ := g.Push(pcm(byte(i)), at(i)); out != nil {
			t.Fatalf("closed gate sent frame %d", i)
		}
	}
}

func TestClosedGateSendsNothing(t *testing.T) {
	g := New(0, 0, 0)
	fill(t, g, 200)
	if _, open := g.IsOpen(); open {
		t.Fatal("gate opened itself")
	}
}

func TestSessionStartsWakeLeadBeforeTheCrossing(t *testing.T) {
	g := New(0, 0, 0)
	fill(t, g, 10)
	// The crossing was frame 6. 260ms back is 20ms into frame 3, so frame 3
	// is sent with its first 60ms silenced, then 4..9 and the live 10.
	s, ok := g.Open(at(6), at(9))
	if !ok || s == 0 {
		t.Fatalf("Open = %d, %v", s, ok)
	}
	out, sess, end := g.Push(pcm(10), at(10))
	if end != nil || sess != s {
		t.Fatalf("session %d end %v", sess, end)
	}
	var got []byte
	for _, f := range out {
		got = append(got, tag(f))
	}
	want := []byte{3, 4, 5, 6, 7, 8, 9, 10}
	if string(got) != string(want) {
		t.Fatalf("sent %v, want %v", got, want)
	}
	pad := FrameBytes * 3 / 4 // 60 of 80ms
	for i, b := range out[0][:pad] {
		if b != 0 {
			t.Fatalf("byte %d of the first frame is %d, want silence", i, b)
		}
	}
	if out[1][0] != 4 {
		t.Fatal("silenced a frame after the first")
	}
	if len(g.frames) > 0 {
		t.Fatal("ring kept frames after open")
	}
}

// The mic hands frames over in pairs stamped microseconds apart, with the
// pairs 130-190ms apart, so the lead is counted in frames from the crossing.
func TestWakeLeadCountsFramesNotTime(t *testing.T) {
	g := New(0, 0, 0)
	batch := t0
	for i := 0; i < 10; i++ {
		if i%2 == 0 {
			batch = batch.Add(190 * time.Millisecond) // a slow batch
		}
		g.Push(pcm(byte(i)), batch.Add(time.Duration(i%2)*time.Microsecond))
	}
	cross := g.frames[6].at
	g.Open(cross, cross)
	out, _, _ := g.Push(pcm(10), cross.Add(time.Second))
	if tag(out[0]) != 3 || len(out) != 8 {
		t.Fatalf("started at frame %d with %d frames, want 3 and 8", tag(out[0]), len(out))
	}
}

// Silencing the first frame must not write into the ring's own buffer, which
// other readers (the bench dump) still hold.
func TestWakeLeadDoesNotTouchTheRingedFrame(t *testing.T) {
	g := New(0, 0, 0)
	frames := make([][]byte, 10)
	for i := range frames {
		frames[i] = pcm(byte(i))
		g.Push(frames[i], at(i))
	}
	g.Open(at(6), at(9))
	if frames[3][0] != 3 {
		t.Fatal("the ringed frame was silenced in place")
	}
}

func TestRingIsBounded(t *testing.T) {
	g := New(time.Second, 0, 0) // 12 frames
	fill(t, g, 100)
	g.Open(t0.Add(-time.Hour), at(100)) // everything in the ring qualifies
	out, _, _ := g.Push(pcm(200), at(100))
	if len(out) != 13 {
		t.Fatalf("sent %d frames, want 12 ringed + the live one", len(out))
	}
	if out[0][0] != 88 {
		t.Fatalf("oldest ringed frame is %d, want 88", out[0][0])
	}
}

func TestWakeInsideOpenSessionDoesNotStartAnother(t *testing.T) {
	g := New(0, 0, 0)
	s, _ := g.Open(at(0), at(0))
	if s2, ok := g.Open(at(3), at(3)); ok || s2 != s {
		t.Fatalf("second Open = %d, %v", s2, ok)
	}
}

func TestUnacknowledgedWakeClosesOnTheDevice(t *testing.T) {
	g := New(0, 3*time.Second, 0)
	s, _ := g.Open(at(0), at(0))
	if out, _, end := g.Push(pcm(1), at(1)); end != nil || len(out) == 0 {
		t.Fatal("closed too early")
	}
	out, _, end := g.Push(pcm(2), t0.Add(3*time.Second))
	if end == nil || end.Session != s || end.Reason != ReasonAckTimeout {
		t.Fatalf("end = %+v", end)
	}
	if out != nil {
		t.Fatal("sent audio after the ack deadline")
	}
}

func TestAckStopsTheAckClockButNotMaxOpen(t *testing.T) {
	g := New(0, 3*time.Second, 10*time.Second)
	s, _ := g.Open(at(0), at(0))
	if !g.Ack(s) {
		t.Fatal("ack refused")
	}
	if _, _, end := g.Push(pcm(1), t0.Add(5*time.Second)); end != nil {
		t.Fatalf("acked session closed at 5s: %+v", end)
	}
	_, _, end := g.Push(pcm(2), t0.Add(10*time.Second))
	if end == nil || end.Reason != ReasonMaxOpen {
		t.Fatalf("end = %+v, want max_open", end)
	}
}

func TestStaleCloseAndAckAreIgnored(t *testing.T) {
	g := New(0, 0, 0)
	s1, _ := g.Open(at(0), at(0))
	g.Close(s1)
	s2, _ := g.Open(at(5), at(5))
	if s1 == s2 {
		t.Fatal("session ids repeat")
	}
	if g.Close(s1) || g.Ack(s1) {
		t.Fatal("a message about a closed session touched the open one")
	}
	if s, open := g.IsOpen(); !open || s != s2 {
		t.Fatal("stale close ended the new session")
	}
}

func TestSessionIdsSkipZeroOnWrap(t *testing.T) {
	g := New(0, 0, 0)
	g.nextID = ^uint32(0)
	s, _ := g.Open(at(0), at(0))
	if s == 0 {
		t.Fatal("session 0 means none on the wire")
	}
}

func TestCloseAnyAndReopenStartsFromTheRing(t *testing.T) {
	g := New(0, 0, 0)
	s, _ := g.Open(at(0), at(0))
	e := g.CloseAny(ReasonMuted)
	if e == nil || e.Session != s || e.Reason != ReasonMuted {
		t.Fatalf("CloseAny = %+v", e)
	}
	if g.CloseAny(ReasonMuted) != nil {
		t.Fatal("closed twice")
	}
	fill(t, g, 3)
	g.Open(at(2), at(3))
	out, _, _ := g.Push(pcm(3), at(3))
	if len(out) != 4 { // the refilled 0, 1, 2 and the live 3
		t.Fatalf("got %d frames", len(out))
	}
}

func TestFloorFollowsQuietAndResistsSpeech(t *testing.T) {
	g := New(0, 0, 0)
	quiet := make([]byte, FrameBytes)
	loud := make([]byte, FrameBytes)
	for i := 0; i < FrameBytes; i += 2 {
		quiet[i] = 30    // ~0.001
		loud[i+1] = 0x40 // ~0.5
	}
	for i := 0; i < 50; i++ {
		g.Push(quiet, at(i))
	}
	base := g.Floor()
	for i := 0; i < 5; i++ {
		g.Push(loud, at(50+i))
	}
	if g.Floor() > base+0.05 {
		t.Fatalf("0.4s of speech dragged the floor from %.4f to %.4f", base, g.Floor())
	}
}
