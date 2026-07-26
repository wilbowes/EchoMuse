package alsa

import (
	"encoding/binary"
	"strings"
	"testing"
)

// TestIoctlNumbers pins the encoded ioctl request numbers against values
// computed by hand from Linux's _IOC macro for a 32-bit userspace.
//
// This is the highest-value test in the package: an off-by-one in the size or
// direction field produces a request number the kernel simply does not
// recognise, and the only symptom on hardware is an opaque EINVAL or ENOTTY
// from a device that looks otherwise healthy.
func TestIoctlNumbers(t *testing.T) {
	// _IOC(dir, type, nr, size) = (dir<<30)|(size<<16)|(type<<8)|nr
	// type 'A' = 0x41, hw_params = 604 bytes (0x25C), sw_params = 104 (0x68).
	tests := []struct {
		name string
		got  uintptr
		want uintptr
	}{
		{"PVERSION  _IOR ('A',0x00,4)", ioctlPVersion, 0x80044100},
		{"HW_REFINE _IOWR('A',0x10,604)", ioctlHWRefine, 0xC25C4110},
		{"HW_PARAMS _IOWR('A',0x11,604)", ioctlHWParams, 0xC25C4111},
		{"HW_FREE   _IO  ('A',0x12)", ioctlHWFree, 0x00004112},
		{"SW_PARAMS _IOWR('A',0x13,104)", ioctlSWParams, 0xC0684113},
		{"PREPARE   _IO  ('A',0x40)", ioctlPrepare, 0x00004140},
		{"START     _IO  ('A',0x42)", ioctlStart, 0x00004142},
		{"DROP      _IO  ('A',0x43)", ioctlDrop, 0x00004143},
	}
	for _, tc := range tests {
		if tc.got != tc.want {
			t.Errorf("%s = %#x, want %#x", tc.name, tc.got, tc.want)
		}
	}
}

// TestHwParamsLayout guards the offsets into snd_pcm_hw_params. They are
// hand-computed from the kernel struct and there is no compiler check on them,
// so a wrong offset would silently write a parameter into a neighbouring
// field.
func TestHwParamsLayout(t *testing.T) {
	// flags(4) + masks[3]*32 + mres[5]*32 = 4 + 256 = 260
	if offIntervals != 260 {
		t.Errorf("offIntervals = %d, want 260", offIntervals)
	}
	// + intervals[12]*12 + ires[9]*12 = 260 + 252 = 512
	if offRmask != 512 {
		t.Errorf("offRmask = %d, want 512", offRmask)
	}
	// The last interval must still sit inside the struct.
	var h hwParams
	if last := h.ivOff(pTickTime) + 12; last > hwParamsSize {
		t.Errorf("last interval ends at %d, past struct size %d", last, hwParamsSize)
	}
	// And the last mask likewise.
	if last := h.maskOff(pSubformat) + 32; last > offIntervals {
		t.Errorf("last mask ends at %d, overlapping intervals at %d", last, offIntervals)
	}
}

// TestParamIndices pins the SNDRV_PCM_HW_PARAM_* values. They are hand-copied
// from the kernel headers with no compile-time check, and one wrong index
// constrains the wrong parameter: pChannels=9 would set FRAME_BITS instead,
// which the hardware accepts and then produces silence.
func TestParamIndices(t *testing.T) {
	tests := []struct {
		name string
		got  int
		want int
	}{
		{"ACCESS", pAccess, 0},
		{"FORMAT", pFormat, 1},
		{"SUBFORMAT", pSubformat, 2},
		{"FIRST_INTERVAL (SAMPLE_BITS)", firstInterval, 8},
		{"CHANNELS", pChannels, 10},
		{"RATE", pRate, 11},
		{"PERIOD_SIZE", pPeriodSize, 13},
		{"PERIOD_BYTES", pPeriodBytes, 14},
		{"PERIODS", pPeriods, 15},
		{"BUFFER_SIZE", pBufferSize, 17},
		{"TICK_TIME", pTickTime, 19},
		{"ACCESS_RW_INTERLEAVED", accessRWInterleaved, 3},
	}
	for _, tc := range tests {
		if tc.got != tc.want {
			t.Errorf("%s = %d, want %d", tc.name, tc.got, tc.want)
		}
	}
}

func TestSetMaskBitIsExclusive(t *testing.T) {
	var h hwParams
	h.setMaskBit(pFormat, int(FormatS16LE))
	h.setMaskBit(pFormat, int(FormatS24_3LE)) // must replace, not accumulate

	got := h.maskBits(pFormat)
	if len(got) != 1 || got[0] != int(FormatS24_3LE) {
		t.Fatalf("maskBits = %v, want exactly [%d], setMaskBit must clear first, "+
			"or a second format request silently leaves the first set",
			got, int(FormatS24_3LE))
	}
}

func TestSetMaskBitAcrossWordBoundary(t *testing.T) {
	// S24_3LE is bit 32: the first bit of the second word, which is exactly
	// where an index/shift mistake shows up.
	var h hwParams
	h.setMaskBit(pFormat, 32)
	base := h.maskOff(pFormat)
	if w0 := binary.LittleEndian.Uint32(h[base:]); w0 != 0 {
		t.Errorf("word0 = %#x, want 0", w0)
	}
	if w1 := binary.LittleEndian.Uint32(h[base+4:]); w1 != 1 {
		t.Errorf("word1 = %#x, want 1", w1)
	}
}

func TestSetExactAndInterval(t *testing.T) {
	var h hwParams
	h.setExact(pChannels, 4)
	h.setExact(pRate, 16000)

	if min, max := h.interval(pChannels); min != 4 || max != 4 {
		t.Errorf("channels = [%d,%d], want [4,4]", min, max)
	}
	if min, max := h.interval(pRate); min != 16000 || max != 16000 {
		t.Errorf("rate = [%d,%d], want [16000,16000]", min, max)
	}
	// The integer flag must be set, otherwise the kernel is free to hand back
	// a fractional refinement.
	o := h.ivOff(pChannels)
	if flags := binary.LittleEndian.Uint32(h[o+8:]); flags&(1<<2) == 0 {
		t.Errorf("integer flag not set on an exact interval (flags=%#x)", flags)
	}
}

// TestAnyIsWideOpen checks the equivalent of snd_pcm_hw_params_any: every mask
// all-ones and every interval full range. If this is too narrow, HW_REFINE
// reports less than the device really supports.
func TestAnyIsWideOpen(t *testing.T) {
	var h hwParams
	h.setExact(pChannels, 2) // dirty it first
	h.any()

	for _, p := range []int{pAccess, pFormat, pSubformat} {
		base := h.maskOff(p)
		for i := 0; i < 8; i++ {
			if v := binary.LittleEndian.Uint32(h[base+i*4:]); v != 0xFFFFFFFF {
				t.Fatalf("mask %d word %d = %#x, want all ones", p, i, v)
			}
		}
	}
	for _, p := range []int{pChannels, pRate, pPeriodSize, pPeriods, pBufferSize} {
		if min, max := h.interval(p); min != 0 || max != 0xFFFFFFFF {
			t.Fatalf("interval %d = [%d,%d], want full range", p, min, max)
		}
	}
	// The kernel refines only parameters whose rmask bit is set. Without this,
	// Capabilities returns whatever was already in the struct and reports it
	// as what the driver supports.
	if v := binary.LittleEndian.Uint32(h[offRmask:]); v != 0xFFFFFFFF {
		t.Errorf("rmask = %#x, want all ones; the driver would refine nothing", v)
	}
	if v := binary.LittleEndian.Uint32(h[offInfo:]); v != 0xFFFFFFFF {
		t.Errorf("info = %#x, want all ones", v)
	}
}

func TestFormatBytes(t *testing.T) {
	tests := []struct {
		f    Format
		want int
	}{
		{FormatS16LE, 2},
		{FormatS24_3LE, 3}, // packed 3-byte, not 4: the whole reason this package exists
		{FormatS24LE, 4},
		{FormatS32LE, 4},
		{Format(999), 0}, // unknown reports 0 so Open can reject it
	}
	for _, tc := range tests {
		if got := tc.f.Bytes(); got != tc.want {
			t.Errorf("%v.Bytes() = %d, want %d", tc.f, got, tc.want)
		}
	}
}

func TestConfigPath(t *testing.T) {
	capture := Config{Card: 0, Device: 22, Playback: false}
	if got, want := capture.Path(), "/dev/snd/pcmC0D22c"; got != want {
		t.Errorf("capture path = %q, want %q", got, want)
	}
	playback := Config{Card: 0, Device: 23, Playback: true}
	if got, want := playback.Path(), "/dev/snd/pcmC0D23p"; got != want {
		t.Errorf("playback path = %q, want %q", got, want)
	}
}

// TestOpenRejectsWrongWordSize documents that the struct sizes above are
// 32-bit-only. CI runs on amd64, where Open must refuse rather than issue
// ioctls with miscomputed sizes.
func TestOpenRejectsWrongWordSize(t *testing.T) {
	if ulongSize == 4 {
		t.Skip("32-bit host: Open would attempt a real device")
	}
	_, err := Open(Config{Card: 0, Device: 22, Channels: 4,
		Format: FormatS24_3LE, Rate: 16000, PeriodSize: 257, Periods: 8})
	if err == nil {
		t.Fatal("Open succeeded on a 64-bit host; it must reject the word size")
	}
	// Assert which error. Without the guard Open still fails on a machine with
	// no such device node, so a bare non-nil check passes either way.
	if !strings.Contains(err.Error(), "32-bit userspace") {
		t.Errorf("got %q, want the word-size rejection; the guard may be gone", err)
	}
}
