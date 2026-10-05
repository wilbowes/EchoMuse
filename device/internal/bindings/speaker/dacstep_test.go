package speaker

import (
	"testing"
	"time"
)

func TestStepDeviationIsFromTheNearestWholePeriod(t *testing.T) {
	per := 42667 * time.Microsecond
	for _, tc := range []struct {
		step, want time.Duration
		ok         bool
	}{
		{per, 0, true},
		{per - 18*time.Millisecond, -18 * time.Millisecond, true},
		{2*per + 5*time.Millisecond, 5 * time.Millisecond, true},
		{-time.Millisecond, 0, false},
		{2 * time.Second, 0, false},
	} {
		got, ok := stepDeviation(tc.step, per)
		if ok != tc.ok || (ok && got != tc.want) {
			t.Errorf("step %v: got %v,%v want %v,%v", tc.step, got, ok, tc.want, tc.ok)
		}
	}
}

func TestCompactStatusIsOneLine(t *testing.T) {
	in := "state: RUNNING\nowner_pid   : 621\ndelay       : 9216\n-----\nhw_ptr      : 123\nappl_ptr    : 9339\n"
	if got := compactStatus(in); got != "state=RUNNING owner_pid=621 delay=9216 hw_ptr=123 appl_ptr=9339" {
		t.Errorf("got %q", got)
	}
}
