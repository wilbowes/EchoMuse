//go:build server

// bf_capture: beamforming + processing pipeline diagnostic tool for the
// Echo Dot Gen 2 (biscuit) 7-microphone array.
//
// Captures N seconds of audio and writes:
//   - out_raw.wav         — best mic, no processing (always written)
//   - out_processed.wav   — pipeline output with active stages
//
// Flags:
//
//	--angle   <deg>   beam steering angle, clockwise from 12 o'clock (default 0)
//	--seconds <1-60>  capture duration (default 5)
//	--bf              enable frequency-domain delay-and-sum beamforming
//	--ns              enable VAD-gated spectral subtraction noise suppression
//	--agc             enable automatic gain control
//
// Examples:
//
//	bf_capture --seconds 5                        # raw best mic only
//	bf_capture --bf --angle 330 --seconds 5       # beamforming only
//	bf_capture --bf --ns --angle 330 --seconds 5  # bf + noise suppression
//	bf_capture --bf --ns --agc --angle 330        # full stack
//
// Mic geometry (confirmed empirically and from PCB traces, 2026-05):
//
//	Ch0 → MK1 → 330°  radius = 36mm
//	Ch1 → MK2 →  30°
//	Ch2 → MK3 →  90°
//	Ch3 → MK4 → 150°
//	Ch4 → MK5 → 210°
//	Ch5 → MK6 → 270°
//	Ch6 → MK7 → centre (omnidirectional)
//	Ch7, Ch8   → unconnected
//
// Build inside echomuse-compiler Docker container:
//
//	go build -tags server -o bf_capture .
package main

import (
	"encoding/binary"
	"flag"
	"fmt"
	"log"
	"math"
	"math/cmplx"
	"os"
	"time"

	"github.com/Binozo/GoTinyAlsa/pkg/pcm"
	"github.com/Binozo/GoTinyAlsa/pkg/tinyalsa"
)

// ── ALSA constants ────────────────────────────────────────────────────────────

const (
	cardNr      = 0
	deviceNr    = 24
	nChannels   = 9
	sampleRate  = 16000
	periodSize  = 512
	periodCount = 5
	byteSample  = 3 // S24_3LE
	frameSize   = nChannels * byteSample

	rawPath   = "/tmp/bf_capture.raw"
	outputDir = "/tmp/"
)

// ── Array geometry ────────────────────────────────────────────────────────────

const (
	micRadius       = 0.036  // metres, confirmed from PCB measurement
	speedOfSound    = 343.0  // m/s
	samplesPerMetre = sampleRate / speedOfSound // ~46.647
)

// micInfo describes a single microphone channel.
type micInfo struct {
	ch    int
	label string
	angle float64 // degrees clockwise from 12 o'clock; -1 = centre
}

// mics lists all 7 microphones in channel order.
// Confirmed empirically 2026-05 via tone injection + analyse_capture.py.
var mics = [7]micInfo{
	{0, "mk1", 330},
	{1, "mk2", 30},
	{2, "mk3", 90},
	{3, "mk4", 150},
	{4, "mk5", 210},
	{5, "mk6", 270},
	{6, "centre", -1},
}

// ── VAD ───────────────────────────────────────────────────────────────────────

const vadThreshold = 0.003 // RMS threshold for NS gating (lower than vad_stream.go
								// because mic levels vary; tune per device)

func isSpeech(samples []float32) bool {
	var sum float64
	for _, s := range samples {
		sum += float64(s) * float64(s)
	}
	return math.Sqrt(sum/float64(len(samples))) >= vadThreshold
}

// ── Noise suppression ─────────────────────────────────────────────────────────

const (
	nsFFTSize      = 512  // matches period size exactly
	nsOverSubAlpha = 1.2  // over-subtraction factor (1.0–3.0; higher = more aggressive)
	nsFloor        = 0.08 // spectral floor — prevents total nulling of frequency bins
	nsAlpha        = 0.95 // noise floor smoothing (0.9–0.99; higher = slower adaptation)
)

// noiseSuppress holds per-bin noise floor estimates.
type noiseSuppress struct {
	noiseFloor []float64 // magnitude per FFT bin
	initialised bool
}

func newNoiseSuppress() *noiseSuppress {
	return &noiseSuppress{
		noiseFloor: make([]float64, nsFFTSize/2+1),
	}
}

// process applies spectral subtraction noise suppression to one period.
// During silence (VAD inactive), updates the noise floor estimate.
// During speech, freezes the estimate and suppresses.
func (ns *noiseSuppress) process(samples []float32, speech bool) []float32 {
	n := len(samples)

	// Apply Hann window to reduce spectral leakage
	windowed := make([]float64, nsFFTSize)
	for i := 0; i < n && i < nsFFTSize; i++ {
		w := 0.5 * (1 - math.Cos(2*math.Pi*float64(i)/float64(nsFFTSize-1)))
		windowed[i] = float64(samples[i]) * w
	}

	// FFT
	spec := fftReal(windowed)
	bins := len(spec)

	if !ns.initialised {
		// Bootstrap noise floor from first period regardless of VAD
		for k := 0; k < bins; k++ {
			ns.noiseFloor[k] = cmplx.Abs(spec[k])
		}
		ns.initialised = true
	}

	if !speech {
		// Update noise floor estimate during silence
		// Smooth across neighbouring bins to reduce musical noise artefacts
		for k := 0; k < bins; k++ {
			mag := cmplx.Abs(spec[k])
			ns.noiseFloor[k] = nsAlpha*ns.noiseFloor[k] + (1-nsAlpha)*mag
		}
		// 3-bin smoothing of noise floor
		smoothed := make([]float64, bins)
		for k := 0; k < bins; k++ {
			if k == 0 {
				smoothed[k] = (ns.noiseFloor[k] + ns.noiseFloor[k+1]) / 2
			} else if k == bins-1 {
				smoothed[k] = (ns.noiseFloor[k-1] + ns.noiseFloor[k]) / 2
			} else {
				smoothed[k] = (ns.noiseFloor[k-1] + ns.noiseFloor[k] + ns.noiseFloor[k+1]) / 3
			}
		}
		copy(ns.noiseFloor, smoothed)
	}

	// Spectral subtraction: suppress each bin
	for k := 0; k < bins; k++ {
		mag := cmplx.Abs(spec[k])
		phase := cmplx.Phase(spec[k])
		suppressed := mag - nsOverSubAlpha*ns.noiseFloor[k]
		if suppressed < nsFloor*mag {
			suppressed = nsFloor * mag
		}
		spec[k] = complex(suppressed*math.Cos(phase), suppressed*math.Sin(phase))
	}

	// IFFT and undo window scaling
	result := ifftReal(spec, nsFFTSize)

	out := make([]float32, n)
	for i := 0; i < n; i++ {
		v := float32(result[i])
		if v > 1.0 {
			v = 1.0
		} else if v < -1.0 {
			v = -1.0
		}
		out[i] = v
	}
	return out
}

// ── AGC ───────────────────────────────────────────────────────────────────────

const (
	agcTargetRMS  = 0.1   // target RMS level (~-20dBFS)
	agcMaxGain    = 20.0  // maximum gain factor
	agcMinGain    = 0.1   // minimum gain factor
	agcAttack     = 0.01  // gain reduction rate (fast attack)
	agcRelease    = 0.001 // gain increase rate (slow release)
)

type agcState struct {
	gain float64
}

func newAGC() *agcState {
	return &agcState{gain: 1.0}
}

func (a *agcState) process(samples []float32) []float32 {
	// Measure RMS of this period
	var sum float64
	for _, s := range samples {
		sum += float64(s) * float64(s)
	}
	rms := math.Sqrt(sum / float64(len(samples)))

	// Adjust gain toward target
	if rms > 0 {
		targetGain := agcTargetRMS / rms
		if targetGain < a.gain {
			// Need to reduce gain — fast attack
			a.gain += agcAttack * (targetGain - a.gain)
		} else {
			// Need to increase gain — slow release
			a.gain += agcRelease * (targetGain - a.gain)
		}
	}

	// Clamp gain
	if a.gain > agcMaxGain {
		a.gain = agcMaxGain
	} else if a.gain < agcMinGain {
		a.gain = agcMinGain
	}

	out := make([]float32, len(samples))
	for i, s := range samples {
		v := s * float32(a.gain)
		if v > 1.0 {
			v = 1.0
		} else if v < -1.0 {
			v = -1.0
		}
		out[i] = v
	}
	return out
}

// ── Beamforming ───────────────────────────────────────────────────────────────

const bfArcDeg = 90.0 // only include mics within this arc of steering angle

// beamform applies frequency-domain delay-and-sum beamforming.
// Delays are applied as exact phase shifts in the frequency domain,
// avoiding the high-frequency rolloff of time-domain interpolation.
// Only mics within bfArcDeg of the steering direction are included.
func beamform(channels [][]float32, steerDeg float64) []float32 {
	n := len(channels[0])
	steerRad := cwNorthToRad(steerDeg)

	maxProj := -math.MaxFloat64

	type proj struct {
		ch   int
		proj float64
	}
	var projs []proj

	for _, m := range mics {
		if m.angle < 0 {
			continue
		}
		if angleDiffDeg(m.angle, steerDeg) > bfArcDeg {
			continue
		}
		micRad := cwNorthToRad(m.angle)
		p := micRadius * math.Cos(steerRad-micRad)
		projs = append(projs, proj{m.ch, p})
		if p > maxProj {
			maxProj = p
		}
	}

	if len(projs) == 0 {
		return channels[0]
	}

	fftSize := nextPow2(n)
	scale := 1.0 / float64(len(projs))

	sumSpec := make([]complex128, fftSize/2+1)

	for _, p := range projs {
		delay := (maxProj - p.proj) * samplesPerMetre

		// Zero-pad to fftSize and FFT
		buf := make([]float64, fftSize)
		for i, v := range channels[p.ch] {
			buf[i] = float64(v)
		}
		spec := fftReal(buf)

		// Apply phase shift: e^(-j*2π*k*delay/fftSize)
		for k := 0; k < len(spec); k++ {
			phase := -2.0 * math.Pi * float64(k) * delay / float64(fftSize)
			shift := complex(math.Cos(phase), math.Sin(phase))
			sumSpec[k] += spec[k] * shift * complex(scale, 0)
		}
	}

	result := ifftReal(sumSpec, fftSize)

	out := make([]float32, n)
	for i := range out {
		v := float32(result[i])
		if v > 1.0 {
			v = 1.0
		} else if v < -1.0 {
			v = -1.0
		}
		out[i] = v
	}
	return out
}

// ── FFT ───────────────────────────────────────────────────────────────────────

func nextPow2(n int) int {
	p := 1
	for p < n {
		p <<= 1
	}
	return p
}

// fftReal computes the real FFT of x and returns bins 0..N/2.
func fftReal(x []float64) []complex128 {
	n := len(x)
	cx := make([]complex128, n)
	for i, v := range x {
		cx[i] = complex(v, 0)
	}
	fftInPlace(cx)
	return cx[:n/2+1]
}

// ifftReal reconstructs a real signal from bins 0..N/2.
func ifftReal(spec []complex128, n int) []float64 {
	cx := make([]complex128, n)
	bins := n/2 + 1
	for k := 0; k < bins && k < len(spec); k++ {
		cx[k] = spec[k]
	}
	for k := 1; k < n/2; k++ {
		cx[n-k] = cmplx.Conj(cx[k])
	}
	// IFFT via conjugate trick
	for i, v := range cx {
		cx[i] = cmplx.Conj(v)
	}
	fftInPlace(cx)
	out := make([]float64, n)
	scale := 1.0 / float64(n)
	for i, v := range cx {
		out[i] = real(cmplx.Conj(v)) * scale
	}
	return out
}

// fftInPlace performs an in-place Cooley-Tukey FFT.
func fftInPlace(x []complex128) {
	n := len(x)
	j := 0
	for i := 1; i < n; i++ {
		bit := n >> 1
		for ; j&bit != 0; bit >>= 1 {
			j ^= bit
		}
		j ^= bit
		if i < j {
			x[i], x[j] = x[j], x[i]
		}
	}
	for length := 2; length <= n; length <<= 1 {
		angle := -2 * math.Pi / float64(length)
		wlen := complex(math.Cos(angle), math.Sin(angle))
		for i := 0; i < n; i += length {
			w := complex(1, 0)
			for k := 0; k < length/2; k++ {
				u := x[i+k]
				v := x[i+k+length/2] * w
				x[i+k] = u + v
				x[i+k+length/2] = u - v
				w *= wlen
			}
		}
	}
}

// ── Geometry helpers ──────────────────────────────────────────────────────────

func cwNorthToRad(deg float64) float64 {
	return (90.0 - deg) * math.Pi / 180.0
}

func angleDiffDeg(a, b float64) float64 {
	diff := math.Mod(math.Abs(a-b), 360.0)
	if diff > 180.0 {
		diff = 360.0 - diff
	}
	return diff
}

// ── Capture ───────────────────────────────────────────────────────────────────

func main() {
	angle   := flag.Float64("angle", 0, "beam steering angle in degrees (clockwise from 12 o'clock)")
	secs    := flag.Int("seconds", 5, "capture duration in seconds (1-60)")
	doBF   := flag.Bool("bf", false, "enable frequency-domain beamforming")
	doNS   := flag.Bool("ns", false, "enable VAD-gated spectral subtraction noise suppression")
	doAGC  := flag.Bool("agc", false, "enable automatic gain control")
	flag.Parse()

	if *secs < 1 || *secs > 60 {
		log.Fatalf("--seconds must be between 1 and 60")
	}

	fmt.Printf("Pipeline: BF=%v  NS=%v  AGC=%v  angle=%.0f°  duration=%ds\n",
		*doBF, *doNS, *doAGC, *angle, *secs)

	fmt.Println("Stopping mixer service...")
	stopMixer()

	fmt.Printf("Opening ALSA card %d device %d\n", cardNr, deviceNr)
	device := tinyalsa.NewDevice(cardNr, deviceNr, pcm.Config{
		Channels:    nChannels,
		SampleRate:  sampleRate,
		PeriodSize:  periodSize,
		PeriodCount: periodCount,
		Format:      tinyalsa.PCM_FORMAT_S24_3LE,
	})

	// Stream to raw file
	f, err := os.Create(rawPath)
	if err != nil {
		log.Fatalf("create raw: %v", err)
	}

	stream := make(chan []byte, 32)
	errCh  := make(chan error, 1)
	go func() {
		if err := device.GetAudioStream(device.DeviceConfig, stream); err != nil {
			errCh <- err
		}
		close(errCh)
	}()

	deadline     := time.After(time.Duration(*secs) * time.Second)
	bytesWritten := 0

	fmt.Printf("Capturing %ds...\n", *secs)
loop:
	for {
		select {
		case <-deadline:
			break loop
		case err := <-errCh:
			if err != nil {
				log.Fatalf("ALSA error: %v", err)
			}
			break loop
		case buf, ok := <-stream:
			if !ok {
				break loop
			}
			n, err := f.Write(buf)
			if err != nil {
				log.Fatalf("write: %v", err)
			}
			bytesWritten += n
		}
	}
	f.Close()

	totalFrames := bytesWritten / frameSize
	fmt.Printf("Captured %d frames (%dms)\n", totalFrames, totalFrames*1000/sampleRate)

	// Read raw back
	raw, err := os.ReadFile(rawPath)
	if err != nil {
		log.Fatalf("read raw: %v", err)
	}

	totalFrames = len(raw) / frameSize

	// Decode all channels into float32 periods
	periodsTotal := totalFrames / periodSize
	allChannels  := make([][]float32, 7)
	for i := range allChannels {
		allChannels[i] = make([]float32, periodsTotal*periodSize)
	}
	for fi := 0; fi < periodsTotal*periodSize; fi++ {
		base := fi * frameSize
		for ci := 0; ci < 7; ci++ {
			off := base + ci*byteSample
			allChannels[ci][fi] = decodeS24(raw[off], raw[off+1], raw[off+2])
		}
	}

	// Per-channel RMS for mapping validation
	fmt.Println("\nPer-channel RMS:")
	bestIdx := 0
	bestRMS := rmsEnergy(allChannels[0])
	for _, m := range mics {
		rms := rmsEnergy(allChannels[m.ch])
		if m.angle >= 0 {
			fmt.Printf("  Ch%d %-8s %5.0f°  RMS=%.6f\n", m.ch, m.label, m.angle, rms)
		} else {
			fmt.Printf("  Ch%d %-8s centre  RMS=%.6f\n", m.ch, m.label, rms)
		}
		if m.angle >= 0 && rms > bestRMS {
			bestRMS = rms
			bestIdx = m.ch
		}
	}
	fmt.Printf("Best mic: Ch%d\n", bestIdx)

	// ── Raw output: best mic, no processing ─────────────────────────────────
	rawOut := channelToPeriods(allChannels[bestIdx], periodsTotal)
	if err := writePeriodWAV(outputDir+"out_raw.wav", rawOut); err != nil {
		log.Fatalf("write raw wav: %v", err)
	}
	fmt.Printf("\nWrote %s (best mic, no processing)\n", outputDir+"out_raw.wav")

	// ── Processed output ────────────────────────────────────────────────────
	ns  := newNoiseSuppress()
	agc := newAGC()

	processed := make([][]float32, periodsTotal)

	for p := 0; p < periodsTotal; p++ {
		// Extract this period for all channels
		periodChannels := make([][]float32, 7)
		for ci := range periodChannels {
			start := p * periodSize
			periodChannels[ci] = allChannels[ci][start : start+periodSize]
		}

		var out []float32

		// Stage 1: Beamforming or best mic
		if *doBF {
			out = beamform(periodChannels, *angle)
		} else {
			out = periodChannels[bestIdx]
		}

		// Stage 2: Noise suppression
		if *doNS {
			speech := isSpeech(out)
			out = ns.process(out, speech)
		}

		// Stage 3: AGC
		if *doAGC {
			out = agc.process(out)
		}

		processed[p] = out
	}

	if err := writePeriodWAV(outputDir+"out_processed.wav", processed); err != nil {
		log.Fatalf("write processed wav: %v", err)
	}

	stages := "none"
	if *doBF || *doNS || *doAGC {
		stages = ""
		if *doBF  { stages += "BF " }
		if *doNS  { stages += "NS " }
		if *doAGC { stages += "AGC" }
	}
	fmt.Printf("Wrote %s (%s)\n", outputDir+"out_processed.wav", stages)
	fmt.Printf("\nPull with: adb pull /tmp/out_raw.wav /tmp/out_processed.wav .\n")
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func channelToPeriods(samples []float32, nPeriods int) [][]float32 {
	periods := make([][]float32, nPeriods)
	for p := 0; p < nPeriods; p++ {
		start := p * periodSize
		periods[p] = samples[start : start+periodSize]
	}
	return periods
}

func rmsEnergy(samples []float32) float64 {
	var sum float64
	for _, s := range samples {
		sum += float64(s) * float64(s)
	}
	return math.Sqrt(sum / float64(len(samples)))
}

func decodeS24(b0, b1, b2 byte) float32 {
	val := int32(b0) | int32(b1)<<8 | int32(b2)<<16
	if val&0x800000 != 0 {
		val |= ^int32(0xFFFFFF)
	}
	return float32(val) / 8388608.0
}

func writePeriodWAV(path string, periods [][]float32) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()

	numSamples := len(periods) * periodSize
	dataSize   := uint32(numSamples * 2)

	write := func(v any) { binary.Write(f, binary.LittleEndian, v) }

	f.WriteString("RIFF")
	write(uint32(36 + dataSize))
	f.WriteString("WAVE")
	f.WriteString("fmt ")
	write(uint32(16))
	write(uint16(1))
	write(uint16(1))
	write(uint32(sampleRate))
	write(uint32(sampleRate * 2))
	write(uint16(2))
	write(uint16(16))
	f.WriteString("data")
	write(dataSize)

	for _, period := range periods {
		for _, s := range period {
			if s > 1.0 { s = 1.0 }
			if s < -1.0 { s = -1.0 }
			write(int16(s * 32767))
		}
	}
	return nil
}
