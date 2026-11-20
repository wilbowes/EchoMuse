package client

import (
	"reflect"
	"testing"
)

// No tap repeats the wake stream's microphone, wherever the last turn was.
func TestTapsSkipTheStreamsOwnMic(t *testing.T) {
	all := []int{2, 3, 0, 1} // spread from the last turn's direction
	for _, c := range []struct {
		sel, n int
		want   []int
	}{
		{2, 3, []int{3, 0, 1}}, // the stream is on the last turn's mic
		{1, 3, []int{2, 3, 0}}, // it is not: the old plan tapped 1, the stream's own
		{0, 2, []int{2, 3}},
		{9, 3, []int{2, 3, 0}}, // a mic outside the spread
	} {
		if got := tapChannels(append(all, all...), c.sel, c.n); !reflect.DeepEqual(got, c.want) {
			t.Errorf("sel %d n %d: %v, want %v", c.sel, c.n, got, c.want)
		}
	}
}
