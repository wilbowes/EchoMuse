package client

import (
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/wakeword/shadow"
)

// ScorerTap, when set (bench raw capture), receives every chunk a wake scorer
// is given: "s" the wake stream, "t0".."tN" the taps. Nil in release.
var ScorerTap func(tag string, b []byte)

// tapScoring scores other microphones during a reply, beside the wake
// stream: the microphone facing the talker heard barge-ins at about twice the
// rate of one facing away (C95, 41C8, 15LE at 3 m, 2026-11), but which one
// faces the talker is only known from the last turn, so n mics spread from
// that direction are scored and any of them may cross. cmd/server installs
// it for a board with MicArray.ReplyTaps (SetTapScorers); none otherwise.
type tapScoring struct {
	mu      sync.Mutex
	scorers []*shadow.Scorer
	active  func() bool
	was     bool
	chans   []int
	bufs    [][]byte
	recent  [][]byte // the wake stream's last primeChunks, to prime taps
	ivaLoud int      // the stream pushIVATaps last left out
}

// primeChunks is the wake stream history a tap starts a reply with: 2s,
// enough for the ~1.5s a cold scorer needs before its first score. Cold, a
// tap was deaf for the start of every reply, and a try there that scored 0.89
// offline on a tap went unheard (41C8, 2026-11-20). The stream's mic heard
// the same room in the moments before the reply, with no echo yet to cancel.
const primeChunks = 25

// noteStream keeps the wake stream's recent chunks for priming the taps.
func (d *DataClient) noteStream(chunk []byte) {
	t := &d.taps
	t.mu.Lock()
	defer t.mu.Unlock()
	if len(t.scorers) == 0 {
		return
	}
	if len(t.recent) >= primeChunks {
		t.recent = append(t.recent[:0], t.recent[1:]...)
	}
	t.recent = append(t.recent, chunk)
}

// SetTapScorers installs the extra scorers and when they run (a reply is
// audible), closing any previous set. nil removes them.
func (d *DataClient) SetTapScorers(sc []*shadow.Scorer, active func() bool) {
	d.taps.mu.Lock()
	old := d.taps.scorers
	d.taps.scorers, d.taps.active, d.taps.was, d.taps.chans = sc, active, false, nil
	d.taps.ivaLoud = -2 // log the first mapping
	d.taps.bufs = make([][]byte, len(sc))
	d.taps.mu.Unlock()
	for _, s := range old {
		s.Close()
	}
}

// TapScorers returns the installed tap scorers, for changing their
// thresholds in place.
func (d *DataClient) TapScorers() []*shadow.Scorer {
	d.taps.mu.Lock()
	defer d.taps.mu.Unlock()
	return append([]*shadow.Scorer(nil), d.taps.scorers...)
}

// tapPlan returns the microphones to tap this period, or nil. sel is the
// microphone the wake stream is scoring, which no tap repeats.
func (d *DataClient) tapPlan(sel int) []int {
	t := &d.taps
	t.mu.Lock()
	defer t.mu.Unlock()
	on := len(t.scorers) > 0 && t.active != nil && t.active()
	nMics := len(t.scorers)
	if d.tb != nil && d.tb.null {
		nMics-- // the last scorer takes the TV null (pushNullTap)
	}
	if d.tb != nil && d.tb.iva {
		nMics = 0 // every scorer takes a separated stream (pushIVATaps)
	}
	if on && !t.was {
		// Every microphone in turn from the last turn's direction, less the
		// one the wake stream scores. Dropping the first of the spread
		// assumed that was it, and when the stream was on another mic one
		// tap scored the stream's own audio and one mic went unheard
		// (41C8, 2026-11-20: a tap's scores equal the stream's to 3 places).
		t.chans = tapChannels(d.beam.SpreadChannels(1<<30), sel, nMics)
		var hist []byte
		for _, c := range t.recent {
			hist = append(hist, c...)
		}
		for i, s := range t.scorers {
			if len(hist) > 0 {
				s.Prime(hist)
			} else {
				s.Reset()
			}
			t.bufs[i] = t.bufs[i][:0]
		}
		log.Printf("[taps] scoring %s over this reply", chanList(t.chans))
	}
	if on && t.was && containsInt(t.chans, sel) {
		// Always-on taps (bench): the stream moved onto a tapped mic, so
		// re-pick the taps rather than score that mic twice.
		t.chans = tapChannels(d.beam.SpreadChannels(1<<30), sel, nMics)
		log.Printf("[taps] stream on mic %d: now scoring %s", sel, chanList(t.chans))
	}
	t.was = on
	if !on {
		return nil
	}
	return t.chans
}

// pushTaps feeds each tap's cancelled audio (already given its make-up) to
// its scorer on 80ms boundaries.
func (d *DataClient) pushTaps(tapped [][]byte) {
	t := &d.taps
	t.mu.Lock()
	defer t.mu.Unlock()
	at := time.Now()
	for i, pcm := range tapped {
		if i >= len(t.scorers) {
			break
		}
		if pcm == nil {
			t.bufs[i] = t.bufs[i][:0]
			continue
		}
		t.feedLocked(i, pcm, at)
	}
}

// pushNullTap feeds the idle TV null to the last tap scorer while the taps
// are scoring (TVNullBenchOn).
func (d *DataClient) pushNullTap(pcm []byte) {
	t := &d.taps
	t.mu.Lock()
	defer t.mu.Unlock()
	if !t.was || len(t.scorers) == 0 {
		return
	}
	t.feedLocked(len(t.scorers)-1, pcm, time.Now())
}

// pushIVATaps feeds the separated streams to the tap scorers while they are
// scoring: scorer i takes the i-th stream other than loudest. A change of
// loudest re-maps the scorers, logged.
func (d *DataClient) pushIVATaps(streams [][]int16, loudest int) {
	t := &d.taps
	t.mu.Lock()
	defer t.mu.Unlock()
	if !t.was || len(t.scorers) == 0 {
		return
	}
	if loudest != t.ivaLoud {
		t.ivaLoud = loudest
		var picked []int
		for k := range streams {
			if k != loudest && len(picked) < len(t.scorers) {
				picked = append(picked, k)
			}
		}
		log.Printf("[iva] taps score streams %v (loudest %d left out)", picked, loudest)
	}
	at := time.Now()
	i := 0
	for k, y := range streams {
		if k == loudest || i >= len(t.scorers) {
			continue
		}
		t.feedLocked(i, s16ToBytes(y), at)
		i++
	}
}

func (t *tapScoring) feedLocked(i int, pcm []byte, at time.Time) {
	t.bufs[i] = append(t.bufs[i], pcm...)
	for len(t.bufs[i]) >= vadOwwChunkBytes {
		chunk := make([]byte, vadOwwChunkBytes)
		copy(chunk, t.bufs[i][:vadOwwChunkBytes])
		t.bufs[i] = t.bufs[i][vadOwwChunkBytes:]
		if ScorerTap != nil {
			ScorerTap(fmt.Sprintf("t%d", i), chunk)
		}
		t.scorers[i].PushBytesAt(chunk, at)
	}
}

// tapChannels is the first n distinct channels of all, in order, without
// sel (a board can have more beam directions than microphones).
func tapChannels(all []int, sel, n int) []int {
	out := make([]int, 0, n)
	seen := map[int]bool{sel: true}
	for _, c := range all {
		if !seen[c] && len(out) < n {
			seen[c] = true
			out = append(out, c)
		}
	}
	return out
}

func chanList(ch []int) string {
	s := ""
	for _, c := range ch {
		s += fmt.Sprintf(" ch%d", c)
	}
	return s[min(1, len(s)):]
}

func containsInt(xs []int, x int) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}
