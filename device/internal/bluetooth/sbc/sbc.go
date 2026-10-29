// Package sbc is the Low Complexity Subband Codec, the one codec every A2DP
// sink must accept (A2DP 1.3 Appendix B).
//
// The encoder is the half that ships: it runs on the Echo for every frame of
// music sent to a Bluetooth speaker. The decoder exists to prove the encoder,
// and to decode frames from the reference libsbc in tests.
//
// 32-bit ARM Go has no hardware integer divide, so the per-sample and
// per-subband paths use shifts, multiplies and float reciprocals only;
// division happens at setup or once per frame.
package sbc

import (
	"errors"
	"fmt"
)

// ChannelMode values are the header's 2-bit channel_mode field.
type ChannelMode uint8

const (
	Mono ChannelMode = iota
	DualChannel
	Stereo
	JointStereo
)

func (m ChannelMode) String() string {
	switch m {
	case Mono:
		return "mono"
	case DualChannel:
		return "dual"
	case Stereo:
		return "stereo"
	case JointStereo:
		return "joint"
	}
	return fmt.Sprintf("mode(%d)", uint8(m))
}

// Allocation values are the header's allocation_method bit.
type Allocation uint8

const (
	Loudness Allocation = iota
	SNR
)

func (a Allocation) String() string {
	if a == SNR {
		return "snr"
	}
	return "loudness"
}

const (
	syncword   = 0x9C
	headerLen  = 4
	maxBitpool = 250 // the header field allows 255; the spec stops at 250
	minBitpool = 2   // A2DP 4.3.2.6
)

// Config is one frame's parameters: everything the header says.
type Config struct {
	SampleRate int // 16000, 32000, 44100 or 48000
	Mode       ChannelMode
	Blocks     int // 4, 8, 12 or 16
	Subbands   int // 4 or 8
	Allocation Allocation
	Bitpool    int
}

func (c Config) String() string {
	return fmt.Sprintf("%dHz %s blocks=%d subbands=%d %s bitpool=%d",
		c.SampleRate, c.Mode, c.Blocks, c.Subbands, c.Allocation, c.Bitpool)
}

// Channels is 1 for mono and 2 otherwise.
func (c Config) Channels() int {
	if c.Mode == Mono {
		return 1
	}
	return 2
}

// SamplesPerFrame is the PCM samples per channel one frame carries.
func (c Config) SamplesPerFrame() int { return c.Blocks * c.Subbands }

// MaxBitpool is the largest bitpool the mode allows: 16 per subband
// for a channel coded alone, 32 per subband for two coded together, never
// above 250. Past it the bit allocation cannot spend the pool.
func MaxBitpool(mode ChannelMode, subbands int) int {
	n := 16 * subbands
	if mode == Stereo || mode == JointStereo {
		n = 32 * subbands
	}
	if n > maxBitpool {
		n = maxBitpool
	}
	return n
}

func freqIndex(rate int) int {
	switch rate {
	case 16000:
		return 0
	case 32000:
		return 1
	case 44100:
		return 2
	case 48000:
		return 3
	}
	return -1
}

var freqByIndex = [4]int{16000, 32000, 44100, 48000}

// Validate reports whether the header can carry c.
func (c Config) Validate() error {
	if freqIndex(c.SampleRate) < 0 {
		return fmt.Errorf("sbc: sample rate %d", c.SampleRate)
	}
	if c.Mode > JointStereo {
		return fmt.Errorf("sbc: channel mode %d", c.Mode)
	}
	switch c.Blocks {
	case 4, 8, 12, 16:
	default:
		return fmt.Errorf("sbc: %d blocks", c.Blocks)
	}
	if c.Subbands != 4 && c.Subbands != 8 {
		return fmt.Errorf("sbc: %d subbands", c.Subbands)
	}
	if c.Allocation > SNR {
		return fmt.Errorf("sbc: allocation %d", c.Allocation)
	}
	if max := MaxBitpool(c.Mode, c.Subbands); c.Bitpool < minBitpool || c.Bitpool > max {
		return fmt.Errorf("sbc: bitpool %d outside %d..%d for %s with %d subbands",
			c.Bitpool, minBitpool, max, c.Mode, c.Subbands)
	}
	return nil
}

// FrameLength is the size in bytes of every frame c produces. The
// AVDTP layer packs whole frames into an MTU with it.
func (c Config) FrameLength() int {
	ch := c.Channels()
	n := headerLen + (4*c.Subbands*ch+7)/8
	var bits int
	switch c.Mode {
	case Mono, DualChannel:
		bits = c.Blocks * ch * c.Bitpool
	case Stereo:
		bits = c.Blocks * c.Bitpool
	case JointStereo:
		bits = c.Subbands + c.Blocks*c.Bitpool
	}
	return n + (bits+7)/8
}

// Bitrate is the encoded rate in bits per second.
func (c Config) Bitrate() int {
	return 8 * c.FrameLength() * c.SampleRate / (c.Subbands * c.Blocks)
}

// header returns bytes 1 and 2 of the frame header.
func (c Config) header() (byte, byte) {
	b := byte(freqIndex(c.SampleRate)) << 6
	b |= byte((c.Blocks>>2)-1) << 4
	b |= byte(c.Mode) << 2
	b |= byte(c.Allocation) << 1
	if c.Subbands == 8 {
		b |= 1
	}
	return b, byte(c.Bitpool)
}

// ErrSync is a frame that does not start with the syncword.
var ErrSync = errors.New("sbc: no syncword")

// ErrCRC is a frame whose header CRC does not match.
var ErrCRC = errors.New("sbc: header crc mismatch")

// ParseHeader reads a frame header and validates it.
func ParseHeader(b []byte) (Config, error) {
	if len(b) < headerLen {
		return Config{}, errors.New("sbc: short header")
	}
	if b[0] != syncword {
		return Config{}, ErrSync
	}
	c := Config{
		SampleRate: freqByIndex[b[1]>>6],
		Blocks:     int((b[1]>>4)&3+1) << 2,
		Mode:       ChannelMode((b[1] >> 2) & 3),
		Allocation: Allocation((b[1] >> 1) & 1),
		Subbands:   4,
		Bitpool:    int(b[2]),
	}
	if b[1]&1 != 0 {
		c.Subbands = 8
	}
	return c, c.Validate()
}

// crcUpdate runs nbits of data, MSB first, through the header CRC: x^8 + x^4
// + x^3 + x^2 + 1, initial value 0x0F. It covers header bytes 1-2,
// then the join bits and scale factors, which need not end on a byte.
func crcUpdate(crc byte, data []byte, nbits int) byte {
	for i := 0; i < nbits; i++ {
		bit := (data[i>>3] >> (7 - uint(i&7))) & 1
		fb := (crc >> 7) ^ bit
		crc <<= 1
		if fb != 0 {
			crc ^= 0x1D
		}
	}
	return crc
}

// frameCRC is the CRC of a frame whose join bits and scale factors are in
// place: bytes 1-2, skip the CRC byte, then sideBits from byte 4.
func frameCRC(frame []byte, sideBits int) byte {
	crc := crcUpdate(0x0F, frame[1:3], 16)
	return crcUpdate(crc, frame[headerLen:], sideBits)
}

// sideBits is the count of join and scale-factor bits after the header.
func (c Config) sideBits() int {
	n := 4 * c.Subbands * c.Channels()
	if c.Mode == JointStereo {
		n += c.Subbands
	}
	return n
}
