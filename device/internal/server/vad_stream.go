package server

import (
	"encoding/binary"
	"math"
	"net/http"
	"os"
	"strconv"

	"github.com/wilbowes/EchoMuse/pkg/mic"
	"github.com/gin-gonic/gin"
)

// VAD configuration — tunable via environment variables.
//
// VAD_CHANNEL      which of the 9 mic channels to use (default 0)
// VAD_THRESHOLD    RMS threshold 0.0–1.0 (default 0.015)
// VAD_SPEECH_MS    how many ms of speech before we open the gate (default 80ms = 1 period)
// VAD_SILENCE_MS   how many ms of silence before we close the gate (default 600ms)
//
// At 16kHz, 512 frames/period: 1 period = 32ms.

const (
	micChannels    = 9
	micByteSample  = 3   // S24_3LE
	micFramePeriod = 512
	micBytePeriod  = micFramePeriod * micChannels * micByteSample // 13824
	owwChunkBytes  = 1280 * 2                                     // 2560 bytes = 80ms at 16kHz S16_LE mono
)

func vadChannel() int {
	if v := os.Getenv("VAD_CHANNEL"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return 0
}

func vadThreshold() float64 {
	if v := os.Getenv("VAD_THRESHOLD"); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return 0.015
}

func vadSpeechPeriods() int {
	if v := os.Getenv("VAD_SPEECH_MS"); v != "" {
		if ms, err := strconv.Atoi(v); err == nil {
			return intMax(1, ms/32)
		}
	}
	return 1 // 1 period = 32ms
}

func vadSilencePeriods() int {
	if v := os.Getenv("VAD_SILENCE_MS"); v != "" {
		if ms, err := strconv.Atoi(v); err == nil {
			return intMax(1, ms/32)
		}
	}
	return 19 // ~600ms
}

func intMax(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// extractMono converts one period of S24_3LE 9-channel PCM to mono S16_LE.
// Takes the upper 2 bytes of each 3-byte sample (drops LSB).
func extractMono(raw []byte, channel int) []byte {
	frameSize := micChannels * micByteSample
	nFrames := len(raw) / frameSize
	offset := channel * micByteSample
	out := make([]byte, nFrames*2)
	for i := 0; i < nFrames; i++ {
		base := i*frameSize + offset
		out[i*2] = raw[base+1]
		out[i*2+1] = raw[base+2]
	}
	return out
}

// periodRMS returns the RMS energy of a mono S16_LE buffer, normalised 0–1.
func periodRMS(mono []byte) float64 {
	n := len(mono) / 2
	if n == 0 {
		return 0
	}
	var sum float64
	for i := 0; i < n; i++ {
		s := int16(binary.LittleEndian.Uint16(mono[i*2:]))
		f := float64(s) / 32768.0
		sum += f * f
	}
	return math.Sqrt(sum / float64(n))
}

// vadStreamHandler streams VAD-gated mono S16_LE audio to the echo_controller.
//
// The stream is always-on but only sends bytes when speech is detected.
// Silence periods are dropped entirely — the consumer receives speech bursts only.
// Each sent chunk is exactly owwChunkBytes (2560 bytes = 80ms = OWW frame size).
//
// Requires the mic backend to implement mic.Subscribable (PcmMicrophone does).
func (s *Server) vadStreamHandler(c *gin.Context) {
	sub, ok := s.mic.(mic.Subscribable)
	if !ok {
		c.AbortWithStatus(http.StatusNotImplemented)
		return
	}

	ch := sub.Subscribe()
	defer sub.Unsubscribe(ch)

	channel      := vadChannel()
	threshold    := vadThreshold()
	speechNeeded := vadSpeechPeriods()
	silenceMax   := vadSilencePeriods()

	speechCount  := 0
	silenceCount := 0
	active       := false
	buf          := make([]byte, 0, owwChunkBytes*4)

	c.Writer.Header().Set("Content-Type", "application/octet-stream")
	c.Writer.WriteHeader(http.StatusOK)
	flusher, canFlush := c.Writer.(http.Flusher)

	ctx := c.Request.Context()

	for {
		select {
		case <-ctx.Done():
			return
		case raw, ok := <-ch:
			if !ok {
				return
			}

			mono   := extractMono(raw, channel)
			rms    := periodRMS(mono)
			speech := rms >= threshold

			if speech {
				silenceCount = 0
				if !active {
					speechCount++
					if speechCount >= speechNeeded {
						active = true
					}
				}
			} else {
				speechCount = 0
				if active {
					silenceCount++
					if silenceCount >= silenceMax {
						active = false
						silenceCount = 0
						// Pad and flush any remaining partial chunk so OWW
						// always sees complete 80ms frames
						if len(buf) > 0 {
							pad := make([]byte, owwChunkBytes-len(buf)%owwChunkBytes)
							buf = append(buf, pad...)
							for len(buf) >= owwChunkBytes {
								if _, err := c.Writer.Write(buf[:owwChunkBytes]); err != nil {
									return
								}
								buf = buf[owwChunkBytes:]
							}
							if canFlush {
								flusher.Flush()
							}
						}
						buf = buf[:0]
					}
				}
			}

			if active {
				buf = append(buf, mono...)
				for len(buf) >= owwChunkBytes {
					if _, err := c.Writer.Write(buf[:owwChunkBytes]); err != nil {
						return
					}
					buf = buf[owwChunkBytes:]
					if canFlush {
						flusher.Flush()
					}
				}
			}
		}
	}
}