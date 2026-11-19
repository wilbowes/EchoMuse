package client

import "testing"

// A locked aligner whose reference is late gets silence of the batch's
// length, so the canceller stays on the loopback instead of rebuilding.
func TestALockedGapIsSilenceNotTheSoftwareTap(t *testing.T) {
	ref, filled := fillRefGap(nil, true, 640)
	if !filled || len(ref) != 640 {
		t.Fatalf("filled=%v len=%d, want true 640", filled, len(ref))
	}
	for i, b := range ref {
		if b != 0 {
			t.Fatalf("byte %d = %d, want silence", i, b)
		}
	}
}

func TestUnlockedStaysNilAndAReferencePassesThrough(t *testing.T) {
	if ref, filled := fillRefGap(nil, false, 640); ref != nil || filled {
		t.Fatalf("unlocked: ref=%v filled=%v, want nil false", ref != nil, filled)
	}
	in := []byte{1, 2, 3, 4}
	if ref, filled := fillRefGap(in, true, 4); &ref[0] != &in[0] || filled {
		t.Fatal("a present reference must pass through untouched")
	}
}
