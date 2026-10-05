package listen

import (
	"testing"
	"time"
)

var t0 = time.Unix(1000, 0)

func at(i int) time.Time { return t0.Add(time.Duration(i) * frameDur) }

func pcm(tag byte) []byte {
	b := make([]byte, FrameBytes)
	b[0] = tag
	return b
}

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

func TestOpenSendsOnlyFramesAfterTheCrossing(t *testing.T) {
	g := New(0, 0, 0)
	fill(t, g, 10)
	// The wake word's last frame was frame 6; frames 7, 8, 9 are the command.
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
		got = append(got, f[0])
	}
	want := []byte{7, 8, 9, 10}
	if string(got) != string(want) {
		t.Fatalf("sent %v, want %v", got, want)
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
	g.Open(at(0), at(3))
	out, _, _ := g.Push(pcm(3), at(3))
	if len(out) != 3 { // frames 1, 2 and the live 3
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
