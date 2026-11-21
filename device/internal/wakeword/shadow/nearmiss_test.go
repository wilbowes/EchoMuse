package shadow

import (
	"sync"
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/internal/wakeword"
)

// A run of frames over the near-miss bar that never crosses reports once,
// with its peak, when it ends; a run that crosses does not.
func TestANearMissReportsOnceAndACrossingDoesNot(t *testing.T) {
	var (
		mu    sync.Mutex
		peaks []float32
		cross int
	)
	inf := &fakeInferer{}
	s := NewScorer(inf, 0.5, func(float32, float32, time.Time) { mu.Lock(); cross++; mu.Unlock() })
	defer s.Close()
	s.SetNearMiss(0.1, func(p float32, _ time.Time) { mu.Lock(); peaks = append(peaks, p); mu.Unlock() })
	inf.set(0, 0)
	pushAll(t, s, wakeword.FeatWindow)
	waitFor(t, "ready", func() bool { return s.Ready() })
	for _, v := range []float32{0.15, 0.32, 0.2, 0.02, 0.02} { // a near-miss peaking at 0.32
		inf.set(v, 0)
		pushAll(t, s, 1)
	}
	for _, v := range []float32{0.3, 0.8, 0.3, 0.0} { // a wake: crosses, no near-miss
		inf.set(v, 0)
		pushAll(t, s, 1)
	}
	time.Sleep(30 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if len(peaks) != 1 || peaks[0] != 0.32 {
		t.Fatalf("near-misses %v, want one at 0.32", peaks)
	}
	if cross != 1 {
		t.Fatalf("%d crossings, want 1", cross)
	}
}

// ScoreClip scores a clip from a clean state, one score per whole frame.
func TestScoreClipScoresEveryFrame(t *testing.T) {
	inf := &fakeInferer{}
	s := NewScorer(inf, 0.5, nil)
	defer s.Close()
	inf.set(0.7, 0)
	n := 40
	sc := s.ScoreClip(make([]int16, n*wakeword.ChunkSamples+100))
	if len(sc) != n {
		t.Fatalf("%d scores for %d frames", len(sc), n)
	}
	if sc[0] != 0 || sc[n-1] != 0.7 {
		t.Fatalf("first %.2f (want 0: not ready), last %.2f (want 0.7)", sc[0], sc[n-1])
	}
}
