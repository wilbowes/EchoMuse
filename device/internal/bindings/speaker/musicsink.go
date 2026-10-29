//go:build server

package speaker

// The music plane can go somewhere other than this speaker: a Bluetooth
// speaker. Voice never does — a reply stays on the Echo the person spoke to.
//
// The sink is fed from the write loop, so it inherits this DAC's pacing: one
// period handed over per period played. It gets the device volume and the
// duck, and none of the voicing or output chain, which describe this
// speaker's driver and not the sink's.

type sinkBox struct{ s MusicSink }

// SetMusicSink installs (or, with nil, removes) the music plane's
// alternative output.
func (p *PcmSpeaker) SetMusicSink(s MusicSink) {
	if s == nil {
		p.sink.Store(nil)
		return
	}
	p.sink.Store(&sinkBox{s})
}

// toSink hands a music period to the sink when it is ready, and reports
// whether it took it. Write loop only.
func (p *PcmSpeaker) toSink(music []byte) bool {
	box := p.sink.Load()
	if box == nil {
		return false
	}
	box.s.Want(music != nil)
	if music == nil || !box.s.Ready() {
		return false
	}
	// The duck is read at its current point on the ramp, which the mix
	// below still steps once this period, so the two never double-step it.
	g := p.vol.targetGain() * float64(p.mixer.Gain()) / float64(unityGain)
	if cap(p.sinkBuf) < len(music) {
		p.sinkBuf = make([]byte, len(music))
	}
	buf := p.sinkBuf[:len(music)]
	scaleS16(buf, music, g)
	box.s.Write(buf)
	return true
}
