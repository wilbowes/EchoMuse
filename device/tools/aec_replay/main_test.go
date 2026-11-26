package main

import "testing"

func TestHardwareRefTail(t *testing.T) {
	for _, tc := range []struct {
		in   int
		want int
		ok   bool
	}{
		{-1, 64, true},
		{64, 64, true},
		{300, 0, false},
		{0, 0, false},
	} {
		got, err := hardwareRefTail(tc.in)
		if tc.ok && (err != nil || got != tc.want) {
			t.Errorf("hardwareRefTail(%d) = %d, %v; want %d, nil", tc.in, got, err, tc.want)
		}
		if !tc.ok && err == nil {
			t.Errorf("hardwareRefTail(%d) was accepted; a tail the hardware reference ignores must be refused", tc.in)
		}
	}
}

func TestResolveMics(t *testing.T) {
	for _, tc := range []struct {
		mic  int
		mics string
		want string
		ok   bool
	}{
		{-1, "", "", true},
		{3, "", "0:3", true},
		{-1, "0:6,12.6:2", "0:6,12.6:2", true},
		{3, "0:6", "", false},
	} {
		got, err := resolveMics(tc.mic, tc.mics)
		if tc.ok && (err != nil || got != tc.want) {
			t.Errorf("resolveMics(%d, %q) = %q, %v; want %q, nil",
				tc.mic, tc.mics, got, err, tc.want)
		}
		if !tc.ok && err == nil {
			t.Errorf("resolveMics(%d, %q) accepted both microphone selections; one must be refused",
				tc.mic, tc.mics)
		}
	}
}
