package sbc

import (
	"errors"
	"fmt"
)

// Capability is the SBC Codec Specific Information Element (A2DP 1.3 4.3.2):
// four octets, each field a bitmask of what the device supports, or with one
// bit set per field, a configuration. Each mask holds its field's bits as
// they sit in the octet, shifted down to bit 0.
type Capability struct {
	SampleRates  uint8 // Freq*
	ChannelModes uint8 // Mode*
	BlockLengths uint8 // Blocks*
	Subbands     uint8 // Subbands*
	Allocations  uint8 // Alloc*
	MinBitpool   uint8
	MaxBitpool   uint8
}

// Octet 0, bits 7-4 and 3-0; octet 1, bits 7-4, 3-2 and 1-0.
const (
	Freq48000 uint8 = 1 << 0
	Freq44100 uint8 = 1 << 1
	Freq32000 uint8 = 1 << 2
	Freq16000 uint8 = 1 << 3

	ModeJoint  uint8 = 1 << 0
	ModeStereo uint8 = 1 << 1
	ModeDual   uint8 = 1 << 2
	ModeMono   uint8 = 1 << 3

	Blocks16 uint8 = 1 << 0
	Blocks12 uint8 = 1 << 1
	Blocks8  uint8 = 1 << 2
	Blocks4  uint8 = 1 << 3

	Subbands8 uint8 = 1 << 0
	Subbands4 uint8 = 1 << 1

	AllocLoudness uint8 = 1 << 0
	AllocSNR      uint8 = 1 << 1
)

// CapabilityLen is the element's length.
const CapabilityLen = 4

// SourceCapability is everything this encoder can produce.
var SourceCapability = Capability{
	SampleRates:  Freq16000 | Freq32000 | Freq44100 | Freq48000,
	ChannelModes: ModeMono | ModeDual | ModeStereo | ModeJoint,
	BlockLengths: Blocks4 | Blocks8 | Blocks12 | Blocks16,
	Subbands:     Subbands4 | Subbands8,
	Allocations:  AllocLoudness | AllocSNR,
	MinBitpool:   minBitpool,
	MaxBitpool:   maxBitpool,
}

// Bytes is the element as sent in AVDTP's Media Codec capability.
func (c Capability) Bytes() []byte {
	return []byte{
		c.SampleRates<<4 | c.ChannelModes&0x0F,
		c.BlockLengths<<4 | (c.Subbands&3)<<2 | c.Allocations&3,
		c.MinBitpool,
		c.MaxBitpool,
	}
}

// ParseCapability reads an element and checks it is one a device may send:
// every field names at least one option and the bitpool range is legal.
func ParseCapability(b []byte) (Capability, error) {
	if len(b) != CapabilityLen {
		return Capability{}, fmt.Errorf("sbc: capability of %d octets, want %d", len(b), CapabilityLen)
	}
	c := Capability{
		SampleRates:  b[0] >> 4,
		ChannelModes: b[0] & 0x0F,
		BlockLengths: b[1] >> 4,
		Subbands:     (b[1] >> 2) & 3,
		Allocations:  b[1] & 3,
		MinBitpool:   b[2],
		MaxBitpool:   b[3],
	}
	return c, c.check()
}

func (c Capability) check() error {
	switch {
	case c.SampleRates == 0:
		return errors.New("sbc: no sampling frequency")
	case c.ChannelModes == 0:
		return errors.New("sbc: no channel mode")
	case c.BlockLengths == 0:
		return errors.New("sbc: no block length")
	case c.Subbands == 0:
		return errors.New("sbc: no subband count")
	case c.Allocations == 0:
		return errors.New("sbc: no allocation method")
	case c.MinBitpool < minBitpool || c.MaxBitpool > maxBitpool || c.MinBitpool > c.MaxBitpool:
		return fmt.Errorf("sbc: bitpool range %d..%d outside %d..%d",
			c.MinBitpool, c.MaxBitpool, minBitpool, maxBitpool)
	}
	return nil
}

// Preference orders, best first, as (mask bit, value) pairs.
var (
	prefRates = [...]struct {
		bit uint8
		v   int
	}{{Freq48000, 48000}, {Freq44100, 44100}, {Freq32000, 32000}, {Freq16000, 16000}}
	prefModes = [...]struct {
		bit uint8
		v   ChannelMode
	}{{ModeJoint, JointStereo}, {ModeStereo, Stereo}, {ModeDual, DualChannel}, {ModeMono, Mono}}
	prefBlocks = [...]struct {
		bit uint8
		v   int
	}{{Blocks16, 16}, {Blocks12, 12}, {Blocks8, 8}, {Blocks4, 4}}
	prefSubs = [...]struct {
		bit uint8
		v   int
	}{{Subbands8, 8}, {Subbands4, 4}}
	prefAlloc = [...]struct {
		bit uint8
		v   Allocation
	}{{AllocLoudness, Loudness}, {AllocSNR, SNR}}
)

// highQualityBitpool is the bitpool aimed for before clamping to what both
// ends allow. A2DP Table 4.7's high-quality settings are 53 (joint, 44.1kHz)
// and 31 (mono, 44.1kHz); 53 is also what Android sends at 48kHz, where the
// table says 51, and stays inside the 512kb/s a sink must accept.
func highQualityBitpool(m ChannelMode) int {
	if m == Mono || m == DualChannel {
		return 31
	}
	return 53
}

// Negotiate picks the best configuration both local and remote support:
// 48kHz before 44.1, joint stereo, 16 blocks, 8 subbands, loudness, and the
// high-quality bitpool clamped to both ranges and the mode's limit. It
// returns the configuration element to send in AVDTP Set Configuration (one
// bit per field; bitpool from the common minimum to the chosen value) and
// the encoder Config.
func Negotiate(local, remote Capability) (Capability, Config, error) {
	if err := local.check(); err != nil {
		return Capability{}, Config{}, fmt.Errorf("local: %w", err)
	}
	if err := remote.check(); err != nil {
		return Capability{}, Config{}, fmt.Errorf("remote: %w", err)
	}
	var conf Capability
	var cfg Config
	common := func(field string, a, b uint8) (uint8, error) {
		if a&b == 0 {
			return 0, fmt.Errorf("sbc: no common %s (local %#x, remote %#x)", field, a, b)
		}
		return a & b, nil
	}
	m, err := common("sampling frequency", local.SampleRates, remote.SampleRates)
	if err != nil {
		return conf, cfg, err
	}
	for _, p := range prefRates {
		if m&p.bit != 0 {
			conf.SampleRates, cfg.SampleRate = p.bit, p.v
			break
		}
	}
	if m, err = common("channel mode", local.ChannelModes, remote.ChannelModes); err != nil {
		return conf, cfg, err
	}
	for _, p := range prefModes {
		if m&p.bit != 0 {
			conf.ChannelModes, cfg.Mode = p.bit, p.v
			break
		}
	}
	if m, err = common("block length", local.BlockLengths, remote.BlockLengths); err != nil {
		return conf, cfg, err
	}
	for _, p := range prefBlocks {
		if m&p.bit != 0 {
			conf.BlockLengths, cfg.Blocks = p.bit, p.v
			break
		}
	}
	if m, err = common("subbands", local.Subbands, remote.Subbands); err != nil {
		return conf, cfg, err
	}
	for _, p := range prefSubs {
		if m&p.bit != 0 {
			conf.Subbands, cfg.Subbands = p.bit, p.v
			break
		}
	}
	if m, err = common("allocation method", local.Allocations, remote.Allocations); err != nil {
		return conf, cfg, err
	}
	for _, p := range prefAlloc {
		if m&p.bit != 0 {
			conf.Allocations, cfg.Allocation = p.bit, p.v
			break
		}
	}
	lo := maxInt(int(local.MinBitpool), int(remote.MinBitpool))
	hi := minInt(minInt(int(local.MaxBitpool), int(remote.MaxBitpool)), MaxBitpool(cfg.Mode, cfg.Subbands))
	if lo > hi {
		return conf, cfg, fmt.Errorf("sbc: no common bitpool (local %d..%d, remote %d..%d, %s limit %d)",
			local.MinBitpool, local.MaxBitpool, remote.MinBitpool, remote.MaxBitpool,
			cfg.Mode, MaxBitpool(cfg.Mode, cfg.Subbands))
	}
	cfg.Bitpool = minInt(maxInt(highQualityBitpool(cfg.Mode), lo), hi)
	conf.MinBitpool, conf.MaxBitpool = uint8(lo), uint8(cfg.Bitpool)
	return conf, cfg, nil
}

// Configuration reads a configuration element (one bit per field, as in Set
// Configuration) as an encoder Config at its maximum bitpool.
func (c Capability) Configuration() (Config, error) {
	if err := c.check(); err != nil {
		return Config{}, err
	}
	one := func(field string, m uint8) error {
		if m&(m-1) != 0 {
			return fmt.Errorf("sbc: configuration names more than one %s (%#x)", field, m)
		}
		return nil
	}
	for _, f := range []struct {
		name string
		m    uint8
	}{{"sampling frequency", c.SampleRates}, {"channel mode", c.ChannelModes},
		{"block length", c.BlockLengths}, {"subband count", c.Subbands}, {"allocation method", c.Allocations}} {
		if err := one(f.name, f.m); err != nil {
			return Config{}, err
		}
	}
	var cfg Config
	for _, p := range prefRates {
		if c.SampleRates == p.bit {
			cfg.SampleRate = p.v
		}
	}
	for _, p := range prefModes {
		if c.ChannelModes == p.bit {
			cfg.Mode = p.v
		}
	}
	for _, p := range prefBlocks {
		if c.BlockLengths == p.bit {
			cfg.Blocks = p.v
		}
	}
	for _, p := range prefSubs {
		if c.Subbands == p.bit {
			cfg.Subbands = p.v
		}
	}
	for _, p := range prefAlloc {
		if c.Allocations == p.bit {
			cfg.Allocation = p.v
		}
	}
	cfg.Bitpool = int(c.MaxBitpool)
	return cfg, cfg.Validate()
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
