package speaker

import (
	"encoding/binary"
	"math"
)

// MusicSink takes the music plane away from the Echo's own speaker.
type MusicSink interface {
	// Want is told every period whether the music plane has audio, so the
	// sink can connect when music starts.
	Want(music bool)
	// Ready says whether music should go to the sink this period.
	Ready() bool
	// Write takes one stereo S16LE period at 48kHz. It must not block and
	// must copy what it keeps.
	Write(period []byte)
}

// scaleS16 writes src × g into dst, saturating.
func scaleS16(dst, src []byte, g float64) {
	if g == 1 {
		copy(dst, src)
		return
	}
	for i := 0; i+1 < len(src); i += 2 {
		v := math.Round(float64(int16(binary.LittleEndian.Uint16(src[i:]))) * g)
		if v > math.MaxInt16 {
			v = math.MaxInt16
		} else if v < math.MinInt16 {
			v = math.MinInt16
		}
		binary.LittleEndian.PutUint16(dst[i:], uint16(int16(v)))
	}
}
