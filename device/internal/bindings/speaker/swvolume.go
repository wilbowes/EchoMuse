package speaker

import (
	"encoding/binary"
	"math"
	"sync/atomic"
)

// Volume is applied here, to the PCM, and the DAC's own digital volume
// (tinymix "PCM Playback Volume") is held at unity whenever audio is live.
// That is how stock FireOS does it — AudioFlinger attenuates, the DAC is
// never written — and it is what lets anything mixed in AFTER this stage
// (the wake sound, #120) play at the same level whatever the user's volume.
// With the volume in the DAC, everything we write is scaled by it and no
// stage of ours can escape it.
//
// It also makes the AEC reference exact: the hardware loopback on Ch7/Ch8
// carries the bytes written to ALSA (measured unchanged across a DAC volume
// cut), so it is now post-volume by construction, with no scalar to track.

// VolumeGain is the linear gain for a device volume level. The law is the
// DAC control's own — 0.5dB per step, unity at 127 — so a level means the
// same loudness it always did and nothing above the device has to change.
// Level 0 is silence rather than the control's -63.5dB floor.
func VolumeGain(level int) float64 {
	if level <= 0 {
		return 0
	}
	if level >= 127 {
		return 1
	}
	return math.Pow(10, float64(level-127)/40)
}

// softVolume applies the volume to one period at a time, ramping from the
// gain it last applied to the target across the period: a step in gain
// mid-waveform is a click, and the DAC's volume control used to hide that by
// soft-stepping in hardware.
//
// target is written from the control plane and read by the ALSA goroutine;
// cur belongs to the ALSA goroutine alone. The zero value is silence, so a
// speaker nobody has told the volume plays nothing rather than full scale.
type softVolume struct {
	target atomic.Uint64 // math.Float64bits of the gain
	cur    float64
}

func (v *softVolume) set(gain float64) { v.target.Store(math.Float64bits(gain)) }

// apply scales one stereo S16LE period in place.
func (v *softVolume) apply(buf []byte) {
	tgt := math.Float64frombits(v.target.Load())
	frames := len(buf) / 4
	if frames == 0 || (tgt == 1 && v.cur == 1) {
		v.cur = tgt
		return
	}
	step := (tgt - v.cur) / float64(frames)
	g := v.cur
	for i := 0; i < frames; i++ {
		g += step
		for c := 0; c < 2; c++ {
			off := i*4 + c*2
			s := float64(int16(binary.LittleEndian.Uint16(buf[off:]))) * g
			binary.LittleEndian.PutUint16(buf[off:], uint16(int16(math.Round(s))))
		}
	}
	v.cur = tgt
}

// settle takes the target without a ramp, for a period of silence, where
// there is nothing to click.
func (v *softVolume) settle() { v.cur = math.Float64frombits(v.target.Load()) }
