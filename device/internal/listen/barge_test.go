package listen

import "testing"

type countingFlusher struct{ n int }

func (f *countingFlusher) Flush() { f.n++ }

func TestABargeOverTheReplyCutsItOnTheEcho(t *testing.T) {
	on, off := true, false
	for _, tc := range []struct {
		name    string
		barge   bool
		bargeIn *bool
		want    int
	}{
		{"barge with barge-in on", true, &on, 1},
		{"a wake with nothing playing", false, &on, 0},
		{"barge-in off: the controller declines it", true, &off, 0},
		{"barge-in never pushed", true, nil, 0},
	} {
		f := &countingFlusher{}
		if got := CutReplyOnBarge(f, tc.barge, tc.bargeIn); got != (tc.want == 1) || f.n != tc.want {
			t.Errorf("%s: cut=%v flushes=%d, want %d", tc.name, got, f.n, tc.want)
		}
	}
}
