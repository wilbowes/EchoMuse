// Package alsa is a dependency-free ALSA PCM client.
//
// It talks SNDRV_PCM_IOCTL_* directly against /dev/snd/pcmC<card>D<dev>{p,c},
// with no cgo and no tinyalsa shared library.
//
// The existing GoTinyAlsa binding handles capture and playback perfectly well,
// S24_3LE included. What it does not expose is HW_REFINE, which is how
// Capabilities asks a driver to describe itself instead of the caller
// assuming. Profile.Verify uses that to fail loudly when a constant disagrees
// with the hardware, and the Echo Show 5's capture constraints were
// established with it rather than guessed. Building without cgo is a secondary
// benefit.
//
// Two ALSA clients in one tree is a cost, not a goal: the intent is to run both
// backends on the same hardware, compare them, and retire one.
//
// Validated against checkers (LineageOS 18.1, kernel 4.9.337, armeabi-v7a).
package alsa

import (
	"encoding/binary"
	"fmt"
	"log"
	"syscall"
	"unsafe"
)

// Format is an SNDRV_PCM_FORMAT_* identifier.
type Format int

const (
	FormatS16LE   Format = 2
	FormatS24LE   Format = 6
	FormatS32LE   Format = 10
	FormatS24_3LE Format = 32
)

// Bytes returns the number of bytes one sample occupies on the wire.
func (f Format) Bytes() int {
	switch f {
	case FormatS16LE:
		return 2
	case FormatS24_3LE:
		return 3
	case FormatS24LE, FormatS32LE:
		return 4
	}
	return 0
}

func (f Format) String() string {
	switch f {
	case FormatS16LE:
		return "S16_LE"
	case FormatS24LE:
		return "S24_LE"
	case FormatS32LE:
		return "S32_LE"
	case FormatS24_3LE:
		return "S24_3LE"
	}
	return fmt.Sprintf("fmt%d", int(f))
}

// ---- ioctl encoding ----

const (
	dirNone  = 0
	dirWrite = 1
	dirRead  = 2
	typeA    = 'A'
)

func ioc(dir, typ, nr, size uintptr) uintptr {
	return (dir << 30) | (size << 16) | (typ << 8) | nr
}

// ulongSize is sizeof(unsigned long), which is what snd_pcm_uframes_t is.
const ulongSize = 32 << (^uint(0) >> 63) / 8

// Struct sizes. Derived for a 32-bit userspace (the Echo Show 5 build is
// armeabi-v7a); Open rejects other word sizes rather than silently
// miscomputing ioctl numbers.
const (
	hwParamsSize = 604
	swParamsSize = 104
)

var (
	ioctlPVersion = ioc(dirRead, typeA, 0x00, 4)
	ioctlHWRefine = ioc(dirRead|dirWrite, typeA, 0x10, hwParamsSize)
	ioctlHWParams = ioc(dirRead|dirWrite, typeA, 0x11, hwParamsSize)
	ioctlHWFree   = ioc(dirNone, typeA, 0x12, 0)
	ioctlSWParams = ioc(dirRead|dirWrite, typeA, 0x13, swParamsSize)
	ioctlPrepare  = ioc(dirNone, typeA, 0x40, 0)
	ioctlStart    = ioc(dirNone, typeA, 0x42, 0)
	ioctlDrop     = ioc(dirNone, typeA, 0x43, 0)
)

// hw_params field offsets: flags | masks[3] | mres[5] | intervals[12] | ires[9] | ...
const (
	offMasks     = 4
	offIntervals = 4 + 8*32 // 260
	offRmask     = 260 + 21*12
	offInfo      = offRmask + 8
)

// mask parameter indices
const (
	pAccess    = 0
	pFormat    = 1
	pSubformat = 2
	nMasks     = 3
)

// interval parameter indices (absolute)
const (
	firstInterval = 8
	pChannels     = 10
	pRate         = 11
	pPeriodSize   = 13
	pPeriodBytes  = 14
	pPeriods      = 15
	pBufferSize   = 17
	pTickTime     = 19
)

const accessRWInterleaved = 3

type hwParams [hwParamsSize]byte

func (h *hwParams) maskOff(p int) int { return offMasks + p*32 }

func (h *hwParams) setMaskBit(p, bit int) {
	base := h.maskOff(p)
	for i := 0; i < 8; i++ {
		binary.LittleEndian.PutUint32(h[base+i*4:], 0)
	}
	word := base + (bit/32)*4
	v := binary.LittleEndian.Uint32(h[word:])
	binary.LittleEndian.PutUint32(h[word:], v|(1<<uint(bit%32)))
}

func (h *hwParams) maskBits(p int) []int {
	base := h.maskOff(p)
	var out []int
	for i := 0; i < 8; i++ {
		v := binary.LittleEndian.Uint32(h[base+i*4:])
		for b := 0; b < 32; b++ {
			if v&(1<<uint(b)) != 0 {
				out = append(out, i*32+b)
			}
		}
	}
	return out
}

func (h *hwParams) ivOff(p int) int { return offIntervals + (p-firstInterval)*12 }

func (h *hwParams) interval(p int) (min, max uint32) {
	o := h.ivOff(p)
	return binary.LittleEndian.Uint32(h[o:]), binary.LittleEndian.Uint32(h[o+4:])
}

func (h *hwParams) setExact(p int, val uint32) {
	o := h.ivOff(p)
	binary.LittleEndian.PutUint32(h[o:], val)
	binary.LittleEndian.PutUint32(h[o+4:], val)
	binary.LittleEndian.PutUint32(h[o+8:], 1<<2) // integer
}

// any mirrors snd_pcm_hw_params_any: every mask all-ones, every interval full.
func (h *hwParams) any() {
	for i := range h {
		h[i] = 0
	}
	for p := 0; p < nMasks; p++ {
		base := h.maskOff(p)
		for i := 0; i < 8; i++ {
			binary.LittleEndian.PutUint32(h[base+i*4:], 0xFFFFFFFF)
		}
	}
	for p := firstInterval; p <= pTickTime; p++ {
		o := h.ivOff(p)
		binary.LittleEndian.PutUint32(h[o:], 0)
		binary.LittleEndian.PutUint32(h[o+4:], 0xFFFFFFFF)
		binary.LittleEndian.PutUint32(h[o+8:], 0)
	}
	binary.LittleEndian.PutUint32(h[offRmask:], 0xFFFFFFFF)
	binary.LittleEndian.PutUint32(h[offInfo:], 0xFFFFFFFF)
}

func ioctl(fd int, req uintptr, arg unsafe.Pointer) error {
	_, _, e := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), req, uintptr(arg))
	if e != 0 {
		return e
	}
	return nil
}

// Config describes a PCM stream to open.
type Config struct {
	Card, Device int
	Playback     bool
	Channels     int
	Format       Format
	Rate         int
	PeriodSize   int // frames
	Periods      int
}

// PCM is an open ALSA stream.
type PCM struct {
	fd            int
	bytesPerFrame int
	bufferFrames  uint32
	// EPIPE recovery counters (read=overrun, write=underrun). One PCM is
	// driven by exactly one goroutine (the mic/speaker binding's own
	// loop), so plain fields are enough — no concurrent caller to race.
	// Added 2026-08-27: recovery here used to be completely silent
	// (return 0, nil, caller just calls again), which is fine for a rare
	// event and useless if it's actually happening constantly — a device
	// stuck in a recovery loop would look identical to a healthy one from
	// this package's own log output alone. Logged every 64th occurrence,
	// same rate-limiting shape the mic binding already uses for dropped
	// subscriber batches, so a real storm is visible without one per
	// occurrence flooding the log.
	epipeReads  uint64
	epipeWrites uint64
}

// Path is the character device backing this stream.
func (c Config) Path() string {
	s := "c"
	if c.Playback {
		s = "p"
	}
	return fmt.Sprintf("/dev/snd/pcmC%dD%d%s", c.Card, c.Device, s)
}

// Caps reports what a PCM device supports, as refined by the driver itself
// rather than assumed by the caller.
type Caps struct {
	Formats                      []Format
	ChannelsMin, ChannelsMax     uint32
	RateMin, RateMax             uint32
	PeriodSizeMin, PeriodSizeMax uint32
	PeriodsMin, PeriodsMax       uint32
}

// Capabilities opens the device briefly and asks the driver to refine a
// wide-open parameter set, which yields the true supported ranges.
func Capabilities(card, device int, playback bool) (*Caps, error) {
	if ulongSize != 4 {
		return nil, fmt.Errorf("alsa: only 32-bit userspace is supported (sizeof(long)=%d)", ulongSize)
	}
	cfg := Config{Card: card, Device: device, Playback: playback}
	fd, err := syscall.Open(cfg.Path(), syscall.O_RDWR, 0)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", cfg.Path(), err)
	}
	defer syscall.Close(fd)

	var hp hwParams
	hp.any()
	if err := ioctl(fd, ioctlHWRefine, unsafe.Pointer(&hp)); err != nil {
		return nil, fmt.Errorf("HW_REFINE: %w", err)
	}
	c := &Caps{}
	// Format IDs above SNDRV_PCM_FORMAT_LAST are not meaningful; the all-ones
	// seeding leaves those bits set, so ignore anything past the real range.
	for _, b := range hp.maskBits(pFormat) {
		if b <= 41 {
			c.Formats = append(c.Formats, Format(b))
		}
	}
	c.ChannelsMin, c.ChannelsMax = hp.interval(pChannels)
	c.RateMin, c.RateMax = hp.interval(pRate)
	c.PeriodSizeMin, c.PeriodSizeMax = hp.interval(pPeriodSize)
	c.PeriodsMin, c.PeriodsMax = hp.interval(pPeriods)
	return c, nil
}

// Open configures and prepares a stream. Capture streams are started
// immediately; playback streams auto-start once the ring fills, because an
// explicit START on an empty playback buffer underruns instantly.
func Open(cfg Config) (*PCM, error) {
	if ulongSize != 4 {
		return nil, fmt.Errorf("alsa: only 32-bit userspace is supported (sizeof(long)=%d)", ulongSize)
	}
	if cfg.Format.Bytes() == 0 {
		return nil, fmt.Errorf("alsa: unsupported format %v", cfg.Format)
	}

	fd, err := syscall.Open(cfg.Path(), syscall.O_RDWR, 0)
	if err != nil {
		return nil, fmt.Errorf("open %s: %w", cfg.Path(), err)
	}
	p := &PCM{fd: fd, bytesPerFrame: cfg.Channels * cfg.Format.Bytes()}

	var ver int32
	_ = ioctl(fd, ioctlPVersion, unsafe.Pointer(&ver))

	var hp hwParams
	hp.any()
	hp.setMaskBit(pAccess, accessRWInterleaved)
	hp.setMaskBit(pFormat, int(cfg.Format))
	hp.setExact(pChannels, uint32(cfg.Channels))
	hp.setExact(pRate, uint32(cfg.Rate))
	hp.setExact(pPeriodSize, uint32(cfg.PeriodSize))
	hp.setExact(pPeriods, uint32(cfg.Periods))
	if err := ioctl(fd, ioctlHWParams, unsafe.Pointer(&hp)); err != nil {
		syscall.Close(fd)
		return nil, fmt.Errorf("HW_PARAMS %s %dch %dHz period=%d n=%d: %w",
			cfg.Format, cfg.Channels, cfg.Rate, cfg.PeriodSize, cfg.Periods, err)
	}
	p.bufferFrames, _ = hp.interval(pBufferSize)

	sw := make([]byte, swParamsSize)
	boundary := p.bufferFrames
	for boundary*2 <= 0x7FFFFFFF-p.bufferFrames {
		boundary *= 2
	}
	startThreshold := uint32(1)
	if cfg.Playback {
		startThreshold = p.bufferFrames
	}
	binary.LittleEndian.PutUint32(sw[4:], 1)                       // period_step
	binary.LittleEndian.PutUint32(sw[12:], uint32(cfg.PeriodSize)) // avail_min
	binary.LittleEndian.PutUint32(sw[20:], startThreshold)
	// A stop_threshold at the boundary lets the stream free-run: the kernel
	// never stops it on an xrun, so the EPIPE recovery in Read and Write is a
	// safety net for unusual states rather than the normal path. Capture
	// overruns overwrite the ring and playback starvation replays stale
	// content, both of which the read and write loops absorb.
	binary.LittleEndian.PutUint32(sw[24:], boundary)
	binary.LittleEndian.PutUint32(sw[36:], boundary)
	binary.LittleEndian.PutUint32(sw[40:], uint32(ver)) // proto
	if err := ioctl(fd, ioctlSWParams, unsafe.Pointer(&sw[0])); err != nil {
		syscall.Close(fd)
		return nil, fmt.Errorf("SW_PARAMS: %w", err)
	}

	if err := p.Prepare(); err != nil {
		syscall.Close(fd)
		return nil, err
	}
	if !cfg.Playback {
		if err := ioctl(fd, ioctlStart, nil); err != nil {
			syscall.Close(fd)
			return nil, fmt.Errorf("START: %w", err)
		}
	}
	return p, nil
}

// Prepare resets the stream after an xrun.
func (p *PCM) Prepare() error {
	if err := ioctl(p.fd, ioctlPrepare, nil); err != nil {
		return fmt.Errorf("PREPARE: %w", err)
	}
	return nil
}

// BufferFrames is the ring size the driver settled on.
func (p *PCM) BufferFrames() int { return int(p.bufferFrames) }

// Read fills buf and returns the number of bytes read. On an overrun it
// re-prepares and restarts once, so callers see a short read rather than a
// dead stream.
//
// snd_pcm_read takes a byte count, converts it to frames internally, and
// converts its result back with frames_to_bytes: so the return value is
// bytes, not frames, despite the frame-oriented ALSA API around it.
func (p *PCM) Read(buf []byte) (int, error) {
	n, err := syscall.Read(p.fd, buf)
	if err == syscall.EPIPE {
		if e := p.Prepare(); e != nil {
			return 0, e
		}
		if e := ioctl(p.fd, ioctlStart, nil); e != nil {
			return 0, fmt.Errorf("restart after overrun: %w", e)
		}
		p.epipeReads++
		if p.epipeReads == 1 || p.epipeReads%64 == 0 {
			log.Printf("[alsa] capture overrun recovered (count=%d) — a caller "+
				"seeing 'no frames' with this counter climbing means the stream "+
				"is thrashing recovery, not silent", p.epipeReads)
		}
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	return n, nil
}

// Write queues buf for playback and returns the number of bytes accepted,
// recovering from underruns. Same byte-oriented return as Read.
func (p *PCM) Write(buf []byte) (int, error) {
	n, err := syscall.Write(p.fd, buf)
	if err == syscall.EPIPE {
		if e := p.Prepare(); e != nil {
			return 0, e
		}
		p.epipeWrites++
		if p.epipeWrites == 1 || p.epipeWrites%64 == 0 {
			log.Printf("[alsa] playback underrun recovered (count=%d)", p.epipeWrites)
		}
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	return n, nil
}

// Close drops any in-flight audio and releases the device.
func (p *PCM) Close() error {
	if p.fd < 0 {
		return nil
	}
	_ = ioctl(p.fd, ioctlDrop, nil)
	_ = ioctl(p.fd, ioctlHWFree, nil)
	err := syscall.Close(p.fd)
	p.fd = -1
	return err
}
