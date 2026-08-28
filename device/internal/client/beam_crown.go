//go:build crown

package client

// crownMic is the mic-pipeline stand-in for crown: no beamformer, because
// there is no perimeter array to steer (4 real capsules across two dies,
// not biscuit's 6-around-1-centre geometry), so MVP stays single-channel.
// It always extracts the same fixed channel and never reports a direction.
//
// The extraction math (24-bit sign-extend, Q12 fixed-point gain, clamp to
// int16 and count clips) is copied from beamformer.extractChannel rather
// than shared with it: that function is unexported and parameterised by
// biscuit's package-level frameSize/byteSample constants, and duplicating
// ~15 lines of arithmetic here is a smaller risk than exporting a generic
// version out of the most delicate file in the tree for a second caller
// with a different channel count.
type crownMic struct {
	clipped uint64
}

// crownChannels/crownByteSample/crownFrameSize/crownMicChannel: crown's mic
// is card0,device22, 6ch/16kHz/S24_3LE (docs/echo-show-8-hardware-map.md).
// ch0 is one of the four live capsules (ch0-3; ch4/ch5 measured as idle TDM
// slots, not a hardware AEC reference — see docs/echo-show-8-hardware-map.md)
// — picked arbitrarily among the four since MVP has no beamformer
// to prefer one, and revisited if it turns out to be a worse-placed capsule
// than another.
const (
	crownChannels   = 6
	crownByteSample = 3 // S24_3LE
	crownFrameSize  = crownChannels * crownByteSample
	crownMicChannel = 0
)

func newBeam() beamEngine { return &crownMic{} }

// Lock/Unlock: nothing to steer, so these are no-ops. A voice turn and the
// always-on wake stream read the identical fixed channel.
func (c *crownMic) Lock(enabled bool) {}
func (c *crownMic) Unlock()           {}

// Process mirrors beamformer.extractChannel bit-for-bit (see the type
// comment for why it's a copy, not a shared call): 24-bit sign-extend, gain
// in Q12 fixed point, clamp to int16 range, count clips. angle is always -1
// — there is no direction concept here, and data.go only acts on angle when
// it's >= 0.
func (c *crownMic) Process(raw []byte, steerAngle float64, gain float64) (mono []byte, angle float64) {
	n := len(raw) / crownFrameSize
	out := make([]byte, n*2)
	offset0 := crownMicChannel * crownByteSample
	gainQ := int64(gain*4096.0 + 0.5)
	for i := 0; i < n; i++ {
		base := i*crownFrameSize + offset0
		if base+2 >= len(raw) {
			break
		}
		val := int32(raw[base]) | int32(raw[base+1])<<8 | int32(raw[base+2])<<16
		if val&0x800000 != 0 {
			val |= ^int32(0xFFFFFF)
		}
		v := (int64(val) * gainQ) >> 20
		if v > 32767 {
			v = 32767
			c.clipped++
		} else if v < -32768 {
			v = -32768
			c.clipped++
		}
		out[i*2] = byte(uint16(v))
		out[i*2+1] = byte(uint16(v) >> 8)
	}
	return out, -1
}

func (c *crownMic) ClippedSamples() uint64 { return c.clipped }
