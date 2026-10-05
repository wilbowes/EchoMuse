package config

import (
	"encoding/json"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain"
)

func push(t *testing.T, d *Device, js string) {
	t.Helper()
	var msg ConfigMessage
	if err := json.Unmarshal([]byte(js), &msg); err != nil {
		t.Fatal(err)
	}
	d.Apply(msg)
}

// The controller pushes these keys under the names em_db stores them by.
// Each has a legitimate zero, so each must survive being set to it.
func TestOutputChainKeysApplyIncludingZero(t *testing.T) {
	d := &Device{}
	push(t, d, `{"eqBands":[1,2,3,4,5,6,7,8],"eqLoudness":true,
		"bassGuardEnabled":false,"bassGuardDb":-12,
		"limiterEnabled":false,"limiterThreshold":-3,"limiterRelease":80}`)
	want := outchain.Params{
		Bands: [8]float64{1, 2, 3, 4, 5, 6, 7, 8}, Loudness: true,
		GuardEnabled: false, GuardDb: -12,
		LimiterEnabled: false, LimiterThresholdDb: -3, LimiterReleaseMs: 80,
	}
	if got := d.OutputChain(); got != want {
		t.Fatalf("got %+v\nwant %+v", got, want)
	}

	push(t, d, `{"eqBands":[0,0,0,0,0,0,0,0],"eqLoudness":false,
		"bassGuardEnabled":true,"bassGuardDb":0,
		"limiterEnabled":true,"limiterThreshold":0}`)
	got := d.OutputChain()
	if got.Bands != [8]float64{} || got.Loudness || !got.GuardEnabled ||
		got.GuardDb != 0 || !got.LimiterEnabled || got.LimiterThresholdDb != 0 {
		t.Fatalf("zeros were not applied: %+v", got)
	}
	if got.LimiterReleaseMs != 80 {
		t.Fatalf("an absent key changed: release %v", got.LimiterReleaseMs)
	}
}

// The values themselves are pinned against em_db by the controller suite
// (test_output_chain_on_device.py); this pins that a device starts on them.
func TestOutputChainStartsAtDefaults(t *testing.T) {
	d := &Device{}
	push(t, d, `{}`)
	if got := d.OutputChain(); got != outchain.DefaultParams() {
		t.Fatalf("got %+v", got)
	}
}

// em_eq pads a short band list with zeros; so do we. A long one is cut.
func TestOutputChainBandsPadAndTruncate(t *testing.T) {
	d := &Device{}
	push(t, d, `{"eqBands":[4,5]}`)
	if got := d.OutputChain().Bands; got != [8]float64{4, 5} {
		t.Fatalf("short: %v", got)
	}
	push(t, d, `{"eqBands":[1,1,1,1,1,1,1,1,9,9]}`)
	if got := d.OutputChain().Bands; got != [8]float64{1, 1, 1, 1, 1, 1, 1, 1} {
		t.Fatalf("long: %v", got)
	}
}
