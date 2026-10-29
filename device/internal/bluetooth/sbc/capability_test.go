package sbc

import (
	"bytes"
	"testing"
)

// The element's octets (A2DP 4.3.2): every field's bits where the spec puts
// them.
func TestCapabilityBytes(t *testing.T) {
	if got := SourceCapability.Bytes(); !bytes.Equal(got, []byte{0xFF, 0xFF, 2, 250}) {
		t.Errorf("source: % x", got)
	}
	for _, tc := range []struct {
		c    Capability
		want []byte
	}{
		{Capability{SampleRates: Freq16000, ChannelModes: ModeMono, BlockLengths: Blocks4,
			Subbands: Subbands4, Allocations: AllocSNR, MinBitpool: 2, MaxBitpool: 2}, []byte{0x88, 0x8A, 2, 2}},
		{Capability{SampleRates: Freq32000, ChannelModes: ModeDual, BlockLengths: Blocks8,
			Subbands: Subbands8, Allocations: AllocLoudness, MinBitpool: 2, MaxBitpool: 250}, []byte{0x44, 0x45, 2, 250}},
		{Capability{SampleRates: Freq44100, ChannelModes: ModeStereo, BlockLengths: Blocks12,
			Subbands: Subbands4, Allocations: AllocLoudness, MinBitpool: 10, MaxBitpool: 53}, []byte{0x22, 0x29, 10, 53}},
		{Capability{SampleRates: Freq48000, ChannelModes: ModeJoint, BlockLengths: Blocks16,
			Subbands: Subbands8, Allocations: AllocLoudness, MinBitpool: 2, MaxBitpool: 53}, []byte{0x11, 0x15, 2, 53}},
	} {
		if got := tc.c.Bytes(); !bytes.Equal(got, tc.want) {
			t.Errorf("%+v: % x, want % x", tc.c, got, tc.want)
		}
		back, err := ParseCapability(tc.want)
		if err != nil || back != tc.c {
			t.Errorf("% x parsed as %+v, %v", tc.want, back, err)
		}
	}
}

func TestParseCapabilityRejects(t *testing.T) {
	for _, tc := range []struct {
		name string
		b    []byte
	}{
		{"three octets", []byte{0xFF, 0xFF, 2}},
		{"five octets", []byte{0xFF, 0xFF, 2, 53, 0}},
		{"no frequency", []byte{0x0F, 0xFF, 2, 53}},
		{"no channel mode", []byte{0xF0, 0xFF, 2, 53}},
		{"no block length", []byte{0xFF, 0x0F, 2, 53}},
		{"no subbands", []byte{0xFF, 0xF3, 2, 53}},
		{"no allocation", []byte{0xFF, 0xFC, 2, 53}},
		{"min bitpool 1", []byte{0xFF, 0xFF, 1, 53}},
		{"max bitpool 251", []byte{0xFF, 0xFF, 2, 251}},
		{"min above max", []byte{0xFF, 0xFF, 54, 53}},
	} {
		if _, err := ParseCapability(tc.b); err == nil {
			t.Errorf("%s accepted", tc.name)
		}
	}
	if _, err := ParseCapability([]byte{0xFF, 0xFF, 2, 250}); err != nil {
		t.Errorf("full range refused: %v", err)
	}
	if _, err := ParseCapability([]byte{0xFF, 0xFF, 53, 53}); err != nil {
		t.Errorf("single bitpool refused: %v", err)
	}
}

// A typical speaker (both mandatory rates, everything else, 2..53) gets the
// A2DP high-quality setting at 48kHz.
func TestNegotiateTypicalSink(t *testing.T) {
	sink, _ := ParseCapability([]byte{0x3F, 0xFF, 2, 53})
	conf, cfg, err := Negotiate(SourceCapability, sink)
	if err != nil {
		t.Fatal(err)
	}
	if want := (Config{48000, JointStereo, 16, 8, Loudness, 53}); cfg != want {
		t.Errorf("config %v, want %v", cfg, want)
	}
	if got := conf.Bytes(); !bytes.Equal(got, []byte{0x11, 0x15, 2, 53}) {
		t.Errorf("configuration element % x", got)
	}
	back, err := conf.Configuration()
	if err != nil || back != cfg {
		t.Errorf("Configuration() = %v, %v; want %v", back, err, cfg)
	}
}

func TestNegotiateFollowsTheRemote(t *testing.T) {
	sink := Capability{SampleRates: Freq44100, ChannelModes: ModeMono | ModeDual, BlockLengths: Blocks4 | Blocks8,
		Subbands: Subbands4, Allocations: AllocSNR, MinBitpool: 2, MaxBitpool: 250}
	_, cfg, err := Negotiate(SourceCapability, sink)
	if err != nil {
		t.Fatal(err)
	}
	// Dual over mono; 31 is the high-quality bitpool for a channel alone.
	if want := (Config{44100, DualChannel, 8, 4, SNR, 31}); cfg != want {
		t.Errorf("config %v, want %v", cfg, want)
	}
}

func TestNegotiateBitpool(t *testing.T) {
	base := Capability{SampleRates: Freq44100, ChannelModes: ModeJoint, BlockLengths: Blocks16,
		Subbands: Subbands8, Allocations: AllocLoudness}
	for _, tc := range []struct {
		name     string
		min, max uint8
		mode     uint8
		subbands uint8
		want     int
		wantMin  uint8
		fail     bool
	}{
		{"remote caps it", 2, 35, ModeJoint, Subbands8, 35, 2, false},
		{"remote floor lifts it", 60, 80, ModeJoint, Subbands8, 60, 60, false},
		{"mono 4 subbands limit 64", 2, 250, ModeMono, Subbands4, 31, 2, false},
		{"floor above the mode limit", 200, 250, ModeMono, Subbands8, 0, 0, true},
	} {
		sink := base
		sink.MinBitpool, sink.MaxBitpool, sink.ChannelModes, sink.Subbands = tc.min, tc.max, tc.mode, tc.subbands
		conf, cfg, err := Negotiate(SourceCapability, sink)
		if tc.fail {
			if err == nil {
				t.Errorf("%s: negotiated %v", tc.name, cfg)
			}
			continue
		}
		if err != nil || cfg.Bitpool != tc.want || conf.MinBitpool != tc.wantMin || int(conf.MaxBitpool) != tc.want {
			t.Errorf("%s: %v conf %d..%d err %v, want bitpool %d from %d",
				tc.name, cfg, conf.MinBitpool, conf.MaxBitpool, err, tc.want, tc.wantMin)
		}
		if err := cfg.Validate(); err != nil {
			t.Errorf("%s: %v", tc.name, err)
		}
	}
}

func TestNegotiateNoCommonGround(t *testing.T) {
	local := Capability{SampleRates: Freq48000, ChannelModes: ModeJoint, BlockLengths: Blocks16,
		Subbands: Subbands8, Allocations: AllocLoudness, MinBitpool: 2, MaxBitpool: 53}
	for _, tc := range []struct {
		name string
		mod  func(*Capability)
	}{
		{"frequency", func(c *Capability) { c.SampleRates = Freq44100 }},
		{"mode", func(c *Capability) { c.ChannelModes = ModeStereo }},
		{"blocks", func(c *Capability) { c.BlockLengths = Blocks4 }},
		{"subbands", func(c *Capability) { c.Subbands = Subbands4 }},
		{"allocation", func(c *Capability) { c.Allocations = AllocSNR }},
		{"bitpool", func(c *Capability) { c.MinBitpool, c.MaxBitpool = 54, 60 }},
		{"invalid remote", func(c *Capability) { c.SampleRates = 0 }},
	} {
		remote := local
		tc.mod(&remote)
		if _, cfg, err := Negotiate(local, remote); err == nil {
			t.Errorf("%s: negotiated %v", tc.name, cfg)
		}
	}
}

func TestConfigurationNeedsOneBitPerField(t *testing.T) {
	c := Capability{SampleRates: Freq44100 | Freq48000, ChannelModes: ModeJoint, BlockLengths: Blocks16,
		Subbands: Subbands8, Allocations: AllocLoudness, MinBitpool: 2, MaxBitpool: 53}
	if _, err := c.Configuration(); err == nil {
		t.Error("two frequencies read as a configuration")
	}
	c.SampleRates = Freq44100
	c.ChannelModes = ModeMono
	c.MaxBitpool = 200 // past mono/8's 128
	if _, err := c.Configuration(); err == nil {
		t.Error("a bitpool past the mode limit read as a configuration")
	}
}
