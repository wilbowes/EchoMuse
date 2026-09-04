package clock

import (
	"testing"
	"time"
)

func ms(t time.Time) int64 { return t.UnixMilli() }

func TestShouldStep(t *testing.T) {
	// A fixed "now" so the test says nothing about how long it took to run.
	now := time.Date(2026, 9, 4, 21, 0, 0, 0, time.UTC)

	cases := []struct {
		name   string
		server int64
		want   bool
	}{
		{
			// The fault this exists for: an Echo with no RTC boots in 2010,
			// so the controller is sixteen years ahead of it.
			name:   "device is sixteen years behind the controller",
			server: ms(now.AddDate(16, 0, 0)),
			want:   true,
		},
		{
			name:   "agreed to the second",
			server: ms(now.Add(400 * time.Millisecond)),
			want:   false,
		},
		{
			// Within the link's own jitter. Stepping here would be churn we
			// could not even verify, and on FireOS it would mean fighting
			// Android's time service over fractions of a second.
			name:   "inside the link's RTT excursions",
			server: ms(now.Add(2 * time.Second)),
			want:   false,
		},
		{
			name:   "just under the threshold",
			server: ms(now.Add(StepThreshold - time.Second)),
			want:   false,
		},
		{
			name:   "at the threshold",
			server: ms(now.Add(StepThreshold)),
			want:   true,
		},
		{
			// Symmetric: a device running fast is as wrong as one running
			// slow, and the correction is the same act.
			name:   "device is ahead of the controller",
			server: ms(now.Add(-2 * time.Hour)),
			want:   true,
		},
		{
			// No field on the message — an older controller, or one that does
			// not send it. "No opinion", never "the epoch": setting a device
			// to 1970 is worse than leaving it in 2010, because 1970 looks
			// like a value somebody chose.
			name:   "controller sent nothing",
			server: 0,
			want:   false,
		},
		{
			name:   "controller sent something nonsensical",
			server: -1,
			want:   false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ShouldStep(now, tc.server); got != tc.want {
				t.Errorf("ShouldStep(%d) = %v, want %v", tc.server, got, tc.want)
			}
		})
	}
}

// The device boots in 2010 and the controller is correct: the whole point.
func TestBootClockIsStepped(t *testing.T) {
	boot := time.Date(2010, 1, 1, 0, 0, 0, 0, time.UTC)
	real := time.Date(2026, 9, 4, 21, 0, 0, 0, time.UTC)
	if !ShouldStep(boot, ms(real)) {
		t.Fatal("a device reading 2010 must be corrected")
	}
	// ...and once corrected, the next ack must leave it alone.
	if ShouldStep(real, ms(real)) {
		t.Fatal("a correct clock must not be stepped again")
	}
}
