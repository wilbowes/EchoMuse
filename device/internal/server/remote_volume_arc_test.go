package server

import "testing"

func ringFrameCount(r *recordingRing) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.frames)
}

func TestRemoteVolumeArcIsOptInAndOnlyShowsForAChange(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)

	s.SetVolume(80)
	if got := ringFrameCount(ring); got != 0 {
		t.Fatalf("default-off remote set painted %d frames", got)
	}

	s.SetRemoteVolumeArc(true)
	s.SetVolume(81)
	if got := ringFrameCount(ring); got != 1 {
		t.Fatalf("enabled remote change painted %d frames, want 1", got)
	}
	if !s.volume.DisplayActive() {
		t.Fatal("enabled remote change did not hold the volume display")
	}

	s.SetVolume(81)
	if got := ringFrameCount(ring); got != 1 {
		t.Fatalf("duplicate remote level repainted the ring: %d frames", got)
	}
	s.volume.CancelDisplay()
}

func TestRemoteVolumeArcDoesNotAffectSeedOrPhysicalButtons(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	s.SetRemoteVolumeArc(true)

	s.SeedVolume(80)
	if got := ringFrameCount(ring); got != 0 {
		t.Fatalf("boot-time seed painted %d frames", got)
	}

	s.VolumeStepUp()
	if got := ringFrameCount(ring); got != 1 {
		t.Fatalf("physical button painted %d frames, want 1", got)
	}
	s.volume.CancelDisplay()
}

func TestRemoteVolumeArcSkipsMutedLevel(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	s.SetRemoteVolumeArc(true)

	s.SetVolume(80)
	if got := ringFrameCount(ring); got != 1 {
		t.Fatalf("enabled non-zero remote change painted %d frames, want 1", got)
	}
	s.volume.CancelDisplay()

	s.SetVolume(0)
	if got := s.VolumeLevel(); got != 0 {
		t.Fatalf("muted remote level was not applied: got %d", got)
	}
	if got := ringFrameCount(ring); got != 1 {
		t.Fatalf("muted remote level repainted the ring: %d frames", got)
	}
	if s.volume.DisplayActive() {
		t.Fatal("muted remote level held the volume display")
	}
}
