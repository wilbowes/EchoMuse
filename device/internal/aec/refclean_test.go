package aec

import "testing"

// A clean reference skips dealias entirely: its searches never run, so its
// counters stay at zero however much reference passes.
func TestACleanReferenceSkipsDealias(t *testing.T) {
	c := New(1)
	c.SetRefClean(true)
	ref := make([]byte, FrameSize*2)
	for i := range ref {
		ref[i] = byte(i * 7)
	}
	for i := 0; i < 50; i++ {
		c.ProcessWithRef(make([]byte, FrameSize*2), ref)
	}
	if c.dealias.misses != 0 || c.dealias.backoffs != 0 || c.dealias.everLocked {
		t.Errorf("dealias ran on a clean reference: misses=%d backoffs=%d", c.dealias.misses, c.dealias.backoffs)
	}
}
