package bluetooth

import (
	"encoding/binary"
	"math"
	"sync"
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bluetooth/avdtp"
	"github.com/wilbowes/EchoMuse/internal/bluetooth/sbc"
	"github.com/wilbowes/EchoMuse/internal/bluetooth/sdp"
)

// sinkSBC is a typical speaker's capability: both rates A2DP requires of a
// sink, every mode, bitpool up to 53.
func sinkSBC() sbc.Capability {
	c := sbc.SourceCapability
	c.SampleRates = sbc.Freq44100 | sbc.Freq48000
	c.MaxBitpool = 53
	return c
}

func sinkRecord() sdp.Record {
	return sdp.Record{Handle: 0x00010001, Attrs: []sdp.Attr{
		{ID: sdp.AttrServiceClassIDList, Value: sdp.Seq(sdp.UUID16(sdp.UUIDAudioSink))},
		{ID: sdp.AttrProtocolDescriptorList, Value: sdp.Seq(
			sdp.Seq(sdp.UUID16(sdp.UUIDL2CAP), sdp.Uint16(sdp.PSMAVDTP)),
			sdp.Seq(sdp.UUID16(sdp.UUIDAVDTP), sdp.Uint16(0x0103)),
		)},
		{ID: sdp.AttrProfileDescriptorList, Value: sdp.Seq(sdp.Seq(sdp.UUID16(sdp.UUIDAdvancedAudio), sdp.Uint16(0x0103)))},
	}}
}

// fakeSink is the speaker's audio side: an SDP server with an A2DP Sink
// record, an AVDTP sink session, and a media channel whose packets it keeps.
type fakeSink struct {
	mu      sync.Mutex
	sess    *avdtp.Session
	sig     *L2Channel
	packets [][]byte
	states  []avdtp.State
}

func (k *fakeSink) newSession(ch *L2Channel) *avdtp.Session {
	cap := sinkSBC()
	s := avdtp.NewSession(avdtp.Config{
		Signal: ch, MTU: ch.MTU(), Type: avdtp.SEPSink,
		Codec: avdtp.Codec{Media: avdtp.MediaAudio, Type: avdtp.CodecSBC, Info: cap.Bytes()},
		Select: func(peer avdtp.Codec) (avdtp.Codec, error) {
			remote, err := sbc.ParseCapability(peer.Info)
			if err != nil {
				return avdtp.Codec{}, err
			}
			chosen, _, err := sbc.Negotiate(cap, remote)
			return avdtp.Codec{Media: avdtp.MediaAudio, Type: avdtp.CodecSBC, Info: chosen.Bytes()}, err
		},
		Accept: func(avdtp.Codec) error { return nil },
		OnState: func(s avdtp.State) {
			k.mu.Lock()
			k.states = append(k.states, s)
			k.mu.Unlock()
		},
	})
	ch.SetOnData(s.Feed)
	return s
}

func (k *fakeSink) attach(sp *fakeSpeaker) {
	server := sdp.NewServer(sinkRecord())
	go func() {
		for ch := range sp.opened {
			ch := ch
			switch {
			case ch.PSM == psmSDP:
				ch.SetOnData(func(req []byte) { _ = ch.Send(server.Handle(req, ch.MTU())) })
			case ch.PSM == psmAVDTP:
				k.mu.Lock()
				first := k.sess == nil
				if first {
					k.sig = ch
				}
				k.mu.Unlock()
				if first {
					s := k.newSession(ch)
					k.mu.Lock()
					k.sess = s
					k.mu.Unlock()
					continue
				}
				ch.SetOnData(func(b []byte) {
					k.mu.Lock()
					k.packets = append(k.packets, append([]byte(nil), b...))
					k.mu.Unlock()
				})
				k.mu.Lock()
				s := k.sess
				k.mu.Unlock()
				s.MediaOpened()
			default:
				_ = ch.Close()
			}
		}
	}()
}

func (k *fakeSink) count() int {
	k.mu.Lock()
	defer k.mu.Unlock()
	return len(k.packets)
}

// tone is n stereo S16LE periods of a 1kHz sine at 48kHz, 512 frames each,
// the write loop's period.
func tone(n int) [][]byte {
	var out [][]byte
	t := 0
	for p := 0; p < n; p++ {
		b := make([]byte, 512*4)
		for i := 0; i < 512; i++ {
			v := int16(12000 * math.Sin(2*math.Pi*1000*float64(t)/48000))
			t++
			binary.LittleEndian.PutUint16(b[i*4:], uint16(v))
			binary.LittleEndian.PutUint16(b[i*4+2:], uint16(v))
		}
		out = append(out, b)
	}
	return out
}

func pairedSpeaker(t *testing.T) (*fakeBR, *fakeSpeaker, *Classic, *A2DP, *fakeSink) {
	t.Helper()
	f := newFakeBR()
	sp := f.speaker(spkA)
	sp.pairable = true
	k := &fakeSink{}
	k.attach(sp)
	var a *A2DP
	c, _ := classicSession(t, f, func(c *Classic) { a = NewA2DP(c) })
	t.Cleanup(a.Close)
	if _, err := c.Pair(spkA, "Lounge"); err != nil {
		t.Fatal(err)
	}
	sp.pairable = false
	return f, sp, c, a, k
}

func TestA2DPStreamsMusicToTheSpeaker(t *testing.T) {
	_, _, _, a, k := pairedSpeaker(t)
	a.Select(spkA, "Lounge")
	if a.Ready() {
		t.Fatal("ready before any connection")
	}
	a.Want(true)
	waitForLong(t, a.Ready, 5*time.Second)
	if s := a.Status(); s.State != A2DPReady || s.Codec == "" || s.BitrateK == 0 {
		t.Fatalf("status %+v", s)
	}
	periods := tone(100) // ~1.07s
	for _, p := range periods {
		a.Write(p)
		time.Sleep(2 * time.Millisecond)
	}
	waitForLong(t, func() bool { return k.count() >= 40 }, 5*time.Second)
	if a.Status().State != A2DPStreaming {
		t.Fatalf("state %s", a.Status().State)
	}

	// Every packet is a valid RTP/SBC media packet, sequence numbers run on
	// and timestamps advance by the samples carried, and the SBC decodes to
	// the tone at 48kHz.
	k.mu.Lock()
	pkts := append([][]byte(nil), k.packets...)
	k.mu.Unlock()
	dec := sbc.NewDecoder()
	pcm := make([]int16, 4096)
	var decoded []int16
	var prev avdtp.MediaPacket
	for i, raw := range pkts {
		m, err := avdtp.ParseMediaPacket(raw)
		if err != nil {
			t.Fatalf("packet %d: %v", i, err)
		}
		if i > 0 {
			if m.Seq != prev.Seq+1 {
				t.Fatalf("sequence %d after %d", m.Seq, prev.Seq)
			}
			if m.Timestamp != prev.Timestamp+uint32(prev.Frames*128) {
				t.Fatalf("timestamp %d after %d (+%d frames)", m.Timestamp, prev.Timestamp, prev.Frames)
			}
		}
		prev = m
		payload := m.Payload
		for f := 0; f < m.Frames; f++ {
			cfg, used, err := dec.Decode(payload, pcm)
			if err != nil {
				t.Fatalf("packet %d frame %d: %v", i, f, err)
			}
			if cfg.SampleRate != 48000 {
				t.Fatalf("sample rate %d", cfg.SampleRate)
			}
			decoded = append(decoded, pcm[:cfg.SamplesPerFrame()*cfg.Channels()]...)
			payload = payload[used:]
		}
	}
	// 1kHz at 48kHz is 2 zero crossings per 48 samples. Count on the first
	// channel past the codec's start-up.
	chans := 2
	var crossings, n int
	for i := chans * 1024; i+chans < len(decoded); i += chans {
		if (decoded[i] < 0) != (decoded[i+chans] < 0) {
			crossings++
		}
		n++
	}
	if got := float64(crossings) * 48000 / 2 / float64(n); got < 950 || got > 1050 {
		t.Fatalf("decoded tone at %.0fHz", got)
	}
}

func TestA2DPAcceptsTheSpeakerConnectingToUs(t *testing.T) {
	f, sp, c, a, k := pairedSpeaker(t)
	a.Select(spkA, "Lounge")
	// The pairing left a link up; the speaker opens signalling, configures
	// and opens the stream as initiator, then opens media.
	waitFor(t, func() bool { return len(c.Links()) == 1 })
	ch, err := sp.sig.Connect(psmAVDTP)
	if err != nil {
		t.Fatal(err)
	}
	s := k.newSession(ch)
	k.mu.Lock()
	k.sess, k.sig = s, ch
	k.mu.Unlock()
	if err := s.Connect(); err != nil {
		t.Fatalf("speaker-initiated set-up: %v", err)
	}
	media, err := sp.sig.Connect(psmAVDTP)
	if err != nil {
		t.Fatalf("media: %v", err)
	}
	s.MediaOpened()
	media.SetOnData(func(b []byte) {
		k.mu.Lock()
		k.packets = append(k.packets, append([]byte(nil), b...))
		k.mu.Unlock()
	})
	waitForLong(t, a.Ready, 3*time.Second)
	for _, p := range tone(30) {
		a.Write(p)
	}
	waitForLong(t, func() bool { return k.count() > 0 }, 3*time.Second)
	_ = f
}

func TestA2DPFallsBackWhenTheSpeakerDrops(t *testing.T) {
	f, sp, _, a, _ := pairedSpeaker(t)
	a.Select(spkA, "Lounge")
	a.Want(true)
	waitForLong(t, a.Ready, 5*time.Second)
	f.dropLink(sp, 0x08)
	waitForLong(t, func() bool { return !a.Ready() }, 3*time.Second)
	if s := a.Status(); s.State != A2DPOffline {
		t.Fatalf("state %s", s.State)
	}
	// Music right after must not page again inside reconnectGap.
	a.Want(true)
	time.Sleep(50 * time.Millisecond)
	if a.Status().State == A2DPConnecting {
		t.Fatal("reconnected inside the gap")
	}
}

func TestA2DPIgnoresAnotherSpeaker(t *testing.T) {
	_, _, c, a, _ := pairedSpeaker(t)
	a.Select(spkB, "Other") // a different speaker is selected
	waitFor(t, func() bool { return len(c.Links()) == 1 })
	l := c.byAddr(spkA)
	closed := make(chan struct{})
	ch := &L2Channel{PSM: psmAVDTP, sig: l.sig, open: make(chan struct{}), gone: closed}
	a.inbound(l, ch)
	select {
	case <-closed:
	default:
		t.Fatal("a channel from an unselected speaker was kept")
	}
}

func TestLocalCapabilityIs48kOnly(t *testing.T) {
	c := localSBC()
	if c.SampleRates != sbc.Freq48000 {
		t.Fatalf("rates %x", c.SampleRates)
	}
	_, cfg, err := sbc.Negotiate(c, sinkSBC())
	if err != nil || cfg.SampleRate != 48000 {
		t.Fatalf("%+v %v", cfg, err)
	}
}

func TestAppendPCM(t *testing.T) {
	p := []byte{0x10, 0x00, 0x30, 0x00, 0xFF, 0xFF, 0x01, 0x00}
	if got := appendPCM(nil, p, 2); len(got) != 4 || got[0] != 16 || got[1] != 48 || got[2] != -1 || got[3] != 1 {
		t.Fatalf("stereo %v", got)
	}
	if got := appendPCM(nil, p, 1); len(got) != 2 || got[0] != 32 || got[1] != 0 {
		t.Fatalf("mono %v", got)
	}
}

func waitForLong(t *testing.T, cond func() bool, d time.Duration) {
	t.Helper()
	deadline := time.Now().Add(d)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("condition never held")
		}
		time.Sleep(5 * time.Millisecond)
	}
}
