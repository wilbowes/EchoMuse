package speaker

import (
	"math"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain"
)

func settledVolume(level int) *softVolume {
	volume := &softVolume{}
	volume.set(VolumeGain(level))
	volume.settle()
	return volume
}

func settledResponse(db float64, volume *softVolume) *responseGain {
	response := &responseGain{}
	response.setDB(db)
	response.settle(volume)
	return response
}

func renderResponse(voice, music []byte, response *responseGain, volume *softVolume, chain *outchain.Chain) []byte {
	frames := len(voice) / 4
	volumeTarget := volume.targetGain()
	period := response.begin(volume, frames, volumeTarget)
	gains := make([]float64, frames)
	period.fillGains(gains)
	var mixer Mixer
	mixer.SetGainImmediate(unityGain)
	wide := mixer.MixResponse(make([]float64, frames*2), voice, music, unityGain, gains)
	if chain != nil {
		chain.ProcessFloat(wide, gains)
	}
	out := make([]byte, len(voice))
	volume.applyFloat(wide, out, volumeTarget)
	period.finish(gains)
	return out
}

func TestResponseGainLevels(t *testing.T) {
	for _, tc := range []struct {
		db, want float64
	}{{0, 1}, {6, 1.9953}, {12, 3.9811}} {
		if got := gainFromDB(tc.db); math.Abs(got-tc.want) > 0.001 {
			t.Errorf("%gdB: got %.4f want %.4f", tc.db, got, tc.want)
		}
	}
}

func TestLowResponseLevelUsesHistoricalPath(t *testing.T) {
	volume := settledVolume(87)
	response := settledResponse(0, volume)
	responsePeriod := response.begin(volume, 8, volume.targetGain())
	gains := make([]float64, 8)
	responsePeriod.fillGains(gains)
	if responsePeriod.boosted(gains) {
		t.Fatal("low response level selected the wide response path")
	}
	for i, gain := range gains {
		if gain != 1 {
			t.Fatalf("frame %d gain %.8f, want unity", i, gain)
		}
	}
}

func TestResponseGainIsAppliedBeforeMix(t *testing.T) {
	volume := settledVolume(87)
	response := settledResponse(12, volume)
	responsePeriod := response.begin(volume, 8, volume.targetGain())
	gains := make([]float64, 8)
	responsePeriod.fillGains(gains)
	var mixer Mixer
	mixer.SetGainImmediate(unityGain)
	wide := mixer.MixResponse(make([]float64, 16), period(8, 10000), period(8, 1000), unityGain, gains)
	if got := wide[14]; got < 40809 || got > 40813 {
		t.Fatalf("pre-mix voice gain got %.1f, want about 40811", got)
	}
	if wide[14] <= math.MaxInt16 {
		t.Fatal("boosted mix was prematurely limited to S16")
	}
}

func TestResponseGainIsRelativeToDeviceVolume(t *testing.T) {
	volume := settledVolume(87) // -20dB = 0.1
	response := settledResponse(12, volume)
	buf := renderResponse(period(8, 4000), nil, response, volume, nil)
	got := sampleAt(buf, 7, 0)
	want := int16(1592) // 4000 * 10^(12/20) * 0.1
	if got < want-2 || got > want+2 {
		t.Fatalf("got %d, want about %d", got, want)
	}
}

func TestHotVoiceDoesNotClipBeforeDeviceVolume(t *testing.T) {
	volume := settledVolume(47) // -40dB = 0.01
	response := settledResponse(12, volume)
	buf := renderResponse(period(8, 30000), nil, response, volume, nil)
	got := sampleAt(buf, 7, 0)
	want := int16(1194) // no intermediate 32767 saturation
	if got < want-2 || got > want+2 {
		t.Fatalf("hot voice was clipped before volume: got %d want about %d", got, want)
	}
}

func TestOutputChainCannotCancelResponseBoost(t *testing.T) {
	lowBuf := period(512, 30000)
	lowChain := outchain.New(48000)
	lowChain.SetActive(true)
	lowChain.Process(lowBuf)
	lowVolume := settledVolume(87)
	lowVolume.apply(lowBuf)

	highChain := outchain.New(48000)
	highChain.SetActive(true)
	highVolume := settledVolume(87)
	highResponse := settledResponse(12, highVolume)
	highBuf := renderResponse(period(512, 30000), nil, highResponse, highVolume, highChain)

	low := float64(sampleAt(lowBuf, 511, 0))
	high := float64(sampleAt(highBuf, 511, 0))
	if ratio := high / low; ratio < 3.9 || ratio > 4.1 {
		t.Fatalf("output chain cancelled response boost: ratio %.3f, want about 3.981", ratio)
	}
}

func TestResponseGainShrinksNearMaximumVolume(t *testing.T) {
	for _, level := range []int{115, 121, 127} {
		volume := settledVolume(level)
		response := settledResponse(12, volume)
		buf := renderResponse(period(8, 4000), nil, response, volume, nil)
		if got := sampleAt(buf, 7, 0); got < 3998 || got > 4002 {
			t.Errorf("level %d: combined gain got sample %d, want 4000", level, got)
		}
	}
}

func TestResponseGainCapsWhileVolumeRamps(t *testing.T) {
	volume := settledVolume(87)
	response := settledResponse(12, volume)
	volume.set(1) // volume-up during a response
	buf := renderResponse(period(128, 4000), nil, response, volume, nil)
	for i := 0; i < 128; i++ {
		if got := sampleAt(buf, i, 0); got > 4002 {
			t.Fatalf("frame %d exceeded unity combined gain: %d", i, got)
		}
	}
}

func TestResponsePeriodUsesOneVolumeTarget(t *testing.T) {
	const frames = 8
	volume := settledVolume(87) // -20dB = 0.1
	response := settledResponse(12, volume)
	volumeTarget := volume.targetGain()
	responsePeriod := response.begin(volume, frames, volumeTarget)
	gains := make([]float64, frames)
	responsePeriod.fillGains(gains)

	// Simulate a control-plane volume update after response gain has been
	// capped but before the period is quantised. It belongs to the next
	// period; using it here would multiply the old +12dB response gain by
	// unity volume and clip every sample.
	volume.set(1)
	var mixer Mixer
	mixer.SetGainImmediate(unityGain)
	wide := mixer.MixResponse(make([]float64, frames*2), period(frames, 30000), nil, unityGain, gains)
	out := make([]byte, frames*4)
	volume.applyFloat(wide, out, volumeTarget)

	for i := 0; i < frames; i++ {
		if got := sampleAt(out, i, 0); got < 11940 || got > 11946 {
			t.Fatalf("frame %d used a later volume target: got %d, want about 11943", i, got)
		}
	}
}

func TestMusicKeepsDeviceVolumeUnderBoostedVoice(t *testing.T) {
	plainVolume := settledVolume(87)
	plain := period(32, 4000)
	plainVolume.apply(plain)

	volume := settledVolume(87)
	response := settledResponse(12, volume)
	got := renderResponse(period(32, 0), period(32, 4000), response, volume, nil)
	for i := 0; i < 32; i++ {
		actual, want := sampleAt(got, i, 0), sampleAt(plain, i, 0)
		if actual < want-1 || actual > want+1 {
			t.Fatalf("frame %d: music moved from %d to %d", i, want, actual)
		}
	}
}
