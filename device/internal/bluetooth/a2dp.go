package bluetooth

// The A2DP source: the Echo's music plane, SBC-encoded, to a paired
// Bluetooth speaker, with AVRCP for the speaker's buttons and volume.
//
// Pacing comes from the Echo's own DAC. Music periods are handed over from
// the speaker write loop, which ALSA paces at 48kHz, so the stream runs at
// the rate it would have played locally and the speaker's jitter buffer
// absorbs the drift between the two clocks. Nothing here keeps time.
//
// Only 48kHz is offered. A2DP requires every SBC sink to take both 44.1 and
// 48kHz (A2DP 1.3 §4.3.2), and 48kHz is what the plane carries, so the Echo
// never resamples.

import (
	"encoding/binary"
	"errors"
	"fmt"
	"log"
	"math/rand"
	"sync"
	"sync/atomic"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bluetooth/avdtp"
	"github.com/wilbowes/EchoMuse/internal/bluetooth/avrcp"
	"github.com/wilbowes/EchoMuse/internal/bluetooth/sbc"
	"github.com/wilbowes/EchoMuse/internal/bluetooth/sdp"
)

const (
	// suspendAfter is how long the stream stays started with no music. A
	// speaker on a started stream keeps its amplifier up and the radio
	// stays busy; suspended, it idles and the LE scan can run.
	suspendAfter = 5 * time.Second

	// reconnectGap is the least time between connect attempts made because
	// music started: a speaker that is off costs a 10s page each time, and
	// paging shares the radio with WiFi.
	reconnectGap = 30 * time.Second

	// periodQueue is how many music periods may wait for the encoder before
	// the newest are dropped (the write loop never waits).
	periodQueue = 16
)

// A2DP's own reject codes for a codec configuration (A2DP 1.3 §5.1.3).
const (
	a2dpNotSupportedCodecType  avdtp.ErrorCode = 0xC2
	a2dpNotSupportedSampleRate avdtp.ErrorCode = 0xC4
	a2dpInvalidCodecParams     avdtp.ErrorCode = 0xC1 // INVALID_CODEC_TYPE: the IE does not parse
)

// A2DP states, as reported.
const (
	A2DPIdle       = "idle"       // no speaker selected, or disabled
	A2DPConnecting = "connecting" // paging, SDP, AVDTP set-up
	A2DPReady      = "ready"      // stream open, suspended
	A2DPStreaming  = "streaming"
	A2DPOffline    = "offline" // selected speaker not reachable
)

// A2DPStatus is what the dashboard shows.
type A2DPStatus struct {
	Addr      string `json:"addr,omitempty"`
	Name      string `json:"name,omitempty"`
	State     string `json:"state"`
	Codec     string `json:"codec,omitempty"`
	BitrateK  int    `json:"bitrateKbps,omitempty"`
	DelayMs   int    `json:"delayMs,omitempty"`
	AbsVolume bool   `json:"absVolume,omitempty"`
	Error     string `json:"error,omitempty"`
	Dropped   uint64 `json:"dropped,omitempty"`
}

// localSBC is our SEP's capability: everything the encoder does, at 48kHz.
func localSBC() sbc.Capability {
	c := sbc.SourceCapability
	c.SampleRates = sbc.Freq48000
	return c
}

// a2dpStream is one speaker's connection: AVDTP signalling and media, and
// AVRCP when the speaker has it.
type a2dpStream struct {
	link  *ClassicLink
	sess  *avdtp.Session
	sig   *L2Channel
	media *L2Channel
	rc    *avrcp.Session
	rcCh  *L2Channel

	mu      sync.Mutex
	cfg     sbc.Config
	haveCfg bool
}

// A2DP streams the music plane to one selected speaker.
type A2DP struct {
	c *Classic

	// OnButton reports a speaker button (AVRCP pass-through), pressed only.
	OnButton func(op string)
	// OnStatus is called on every state change. It must not block.
	OnStatus func(A2DPStatus)

	mu          sync.Mutex
	target      string // the selected speaker; "" is none
	targetName  string
	st          *a2dpStream
	state       string
	lastErr     string
	lastAttempt time.Time
	connecting  bool

	periods chan []byte
	dropped atomic.Uint64
	ready   atomic.Bool // a stream is open: music goes to the speaker
	stop    chan struct{}
}

// NewA2DP attaches the source to c: inbound AVDTP and AVCTP channels from a
// paired speaker come here, and our SDP records are served.
func NewA2DP(c *Classic) *A2DP {
	a := &A2DP{c: c, state: A2DPIdle, periods: make(chan []byte, periodQueue), stop: make(chan struct{})}
	server := sdp.NewServer(sdp.DefaultRecords()...)
	c.SDP = server.Handle
	c.OnChannel = a.inbound
	prevDown := c.OnLinkDown
	c.OnLinkDown = func(addr string, reason byte) {
		a.linkDown(addr)
		if prevDown != nil {
			prevDown(addr, reason)
		}
	}
	go a.encodeLoop()
	return a
}

// Select chooses the speaker music goes to; "" stops using one. The speaker
// must be paired. The connection is made when music next plays, or when the
// speaker connects to us.
func (a *A2DP) Select(addr, name string) {
	addr, _ = normaliseAddr(addr)
	a.mu.Lock()
	changed := addr != a.target
	a.target, a.targetName = addr, name
	a.lastAttempt = time.Time{}
	st := a.st
	if changed {
		a.st = nil
	}
	a.mu.Unlock()
	if changed && st != nil {
		a.teardown(st)
	}
	if addr == "" {
		a.setState(A2DPIdle, "")
	}
}

// Ready reports whether music should go to the speaker rather than the
// Echo's own. Called by the write loop every period.
func (a *A2DP) Ready() bool { return a.ready.Load() }

// Write hands over one music period: stereo S16LE at 48kHz. It never blocks;
// a period that finds the queue full is dropped and counted.
func (a *A2DP) Write(period []byte) {
	select {
	case a.periods <- append([]byte(nil), period...):
	default:
		a.dropped.Add(1)
	}
}

// Want is told, every period, whether the music plane has anything in it.
// Music starting with a speaker selected but not connected starts a
// connection, at most once per reconnectGap.
func (a *A2DP) Want(music bool) {
	if !music {
		return
	}
	a.mu.Lock()
	go1 := a.target != "" && a.st == nil && !a.connecting && time.Since(a.lastAttempt) >= reconnectGap
	if go1 {
		a.connecting, a.lastAttempt = true, time.Now()
	}
	target := a.target
	a.mu.Unlock()
	if go1 {
		go a.connect(target)
	}
}

// Status reports the current state.
func (a *A2DP) Status() A2DPStatus {
	a.mu.Lock()
	s := A2DPStatus{Addr: a.target, Name: a.targetName, State: a.state, Error: a.lastErr, Dropped: a.dropped.Load()}
	st := a.st
	a.mu.Unlock()
	if st != nil {
		st.mu.Lock()
		if st.haveCfg {
			s.Codec = fmt.Sprintf("SBC %dkHz %s bitpool %d", st.cfg.SampleRate/1000, st.cfg.Mode, st.cfg.Bitpool)
			s.BitrateK = st.cfg.Bitrate() / 1000
		}
		st.mu.Unlock()
		if d, ok := st.sess.Delay(); ok {
			s.DelayMs = int(d) / 10
		}
		s.AbsVolume = st.rc != nil
	}
	return s
}

// SetVolume sets the speaker's own volume (0-100) when it supports AVRCP
// absolute volume. It reports false when it does not, so the caller can
// scale the audio instead.
func (a *A2DP) SetVolume(pct int) bool {
	a.mu.Lock()
	st := a.st
	a.mu.Unlock()
	if st == nil || st.rc == nil {
		return false
	}
	go func() {
		if _, err := st.rc.SetAbsoluteVolume(avrcp.VolumeToAbs(pct)); err != nil {
			log.Printf("[a2dp] set volume: %v", err)
		}
	}()
	return true
}

func (a *A2DP) setState(state, errText string) {
	a.mu.Lock()
	if a.state == state && a.lastErr == errText {
		a.mu.Unlock()
		return
	}
	a.state, a.lastErr = state, errText
	a.mu.Unlock()
	a.ready.Store(state == A2DPReady || state == A2DPStreaming)
	log.Printf("[a2dp] %s%s", state, map[bool]string{true: ": " + errText, false: ""}[errText != ""])
	if f := a.OnStatus; f != nil {
		f(a.Status())
	}
}

// connect brings up the whole stream to addr as initiator.
func (a *A2DP) connect(addr string) {
	defer func() {
		a.mu.Lock()
		a.connecting = false
		a.mu.Unlock()
	}()
	a.setState(A2DPConnecting, "")
	st, err := a.dial(addr)
	if err != nil {
		state := A2DPOffline
		a.setState(state, err.Error())
		return
	}
	a.mu.Lock()
	if a.target != addr || a.st != nil {
		a.mu.Unlock()
		a.teardown(st)
		return
	}
	a.st = st
	a.mu.Unlock()
	a.setState(A2DPReady, "")
}

func (a *A2DP) dial(addr string) (*a2dpStream, error) {
	l, err := a.c.Connect(addr)
	if err != nil {
		return nil, err
	}
	sink, rcInfo := a.discover(l)
	st := &a2dpStream{link: l}
	sig, err := l.Connect(psmAVDTP)
	if err != nil {
		return nil, fmt.Errorf("AVDTP: %w", err)
	}
	st.sig = sig
	st.sess = a.newSession(st, sig, sink.AVDTPVersion)
	if err := st.sess.Connect(); err != nil {
		_ = sig.Close()
		return nil, fmt.Errorf("stream set-up: %w", err)
	}
	media, err := l.Connect(psmAVDTP)
	if err != nil {
		_ = st.sess.Abort()
		_ = sig.Close()
		return nil, fmt.Errorf("media channel: %w", err)
	}
	st.media = media
	st.sess.MediaOpened()
	if rcInfo.Target && rcInfo.AVCTPPSM != 0 {
		if ch, err := l.Connect(rcInfo.AVCTPPSM); err == nil {
			a.attachRC(st, ch, rcInfo.AbsoluteVolume())
		} else {
			log.Printf("[a2dp] AVRCP not connected: %v", err)
		}
	}
	return st, nil
}

// discover reads the speaker's A2DP and AVRCP records. A speaker that will
// not answer SDP still gets a stream attempt with defaults: SDP failing is
// common and AVDTP's own discovery is what decides.
func (a *A2DP) discover(l *ClassicLink) (sdp.SinkInfo, sdp.AVRCPInfo) {
	var sink sdp.SinkInfo
	var rc sdp.AVRCPInfo
	ch, err := l.Connect(psmSDP)
	if err != nil {
		log.Printf("[a2dp] SDP unavailable: %v", err)
		return sink, rc
	}
	defer ch.Close()
	cl := sdp.NewClient(newL2Requester(ch))
	if s, err := cl.FindA2DPSink(); err == nil {
		sink = s
	} else {
		log.Printf("[a2dp] no A2DP sink record: %v", err)
	}
	if infos, err := cl.FindAVRCP(); err == nil {
		for _, i := range infos {
			if i.Target {
				rc = i
				break
			}
		}
	}
	return sink, rc
}

func (a *A2DP) newSession(st *a2dpStream, sig *L2Channel, peerVersion uint16) *avdtp.Session {
	local := localSBC()
	sess := avdtp.NewSession(avdtp.Config{
		Signal:         sig,
		MTU:            sig.MTU(),
		Codec:          avdtp.Codec{Media: avdtp.MediaAudio, Type: avdtp.CodecSBC, Info: local.Bytes()},
		DelayReporting: true,
		PeerVersion:    peerVersion,
		Select: func(peer avdtp.Codec) (avdtp.Codec, error) {
			if peer.Type != avdtp.CodecSBC {
				return avdtp.Codec{}, errors.New("not SBC")
			}
			remote, err := sbc.ParseCapability(peer.Info)
			if err != nil {
				return avdtp.Codec{}, err
			}
			chosen, cfg, err := sbc.Negotiate(local, remote)
			if err != nil {
				return avdtp.Codec{}, err
			}
			st.setConfig(cfg)
			return avdtp.Codec{Media: avdtp.MediaAudio, Type: avdtp.CodecSBC, Info: chosen.Bytes()}, nil
		},
		Accept: func(cfg avdtp.Codec) error {
			if cfg.Type != avdtp.CodecSBC {
				return &avdtp.CodeError{Code: a2dpNotSupportedCodecType}
			}
			c, err := sbc.ParseCapability(cfg.Info)
			if err != nil {
				return &avdtp.CodeError{Code: a2dpInvalidCodecParams}
			}
			conf, err := c.Configuration()
			if err != nil || conf.SampleRate != 48000 {
				return &avdtp.CodeError{Code: a2dpNotSupportedSampleRate}
			}
			st.setConfig(conf)
			return nil
		},
		OnState: func(s avdtp.State) { a.streamState(st, s) },
	})
	sig.SetOnData(sess.Feed)
	return sess
}

func (st *a2dpStream) setConfig(cfg sbc.Config) {
	st.mu.Lock()
	st.cfg, st.haveCfg = cfg, true
	st.mu.Unlock()
}

func (st *a2dpStream) config() (sbc.Config, bool) {
	st.mu.Lock()
	defer st.mu.Unlock()
	return st.cfg, st.haveCfg
}

func (a *A2DP) attachRC(st *a2dpStream, ch *L2Channel, absVolume bool) {
	rc := avrcp.NewSession(ch, avrcp.Config{
		MTU: ch.MTU(),
		OnPassThrough: func(op avrcp.Op, pressed bool) {
			if pressed && a.OnButton != nil {
				a.OnButton(opName(op))
			}
		},
	})
	ch.SetOnData(func(b []byte) { _ = rc.Receive(b) })
	st.mu.Lock()
	st.rcCh = ch
	if absVolume {
		st.rc = rc
	}
	st.mu.Unlock()
}

func opName(op avrcp.Op) string {
	switch op {
	case avrcp.OpPlay:
		return "play"
	case avrcp.OpPause:
		return "pause"
	case avrcp.OpStop:
		return "stop"
	case avrcp.OpForward:
		return "next"
	case avrcp.OpBackward:
		return "previous"
	case avrcp.OpVolumeUp:
		return "volume_up"
	case avrcp.OpVolumeDown:
		return "volume_down"
	case avrcp.OpMute:
		return "mute"
	}
	return fmt.Sprintf("0x%02x", uint8(op))
}

func (a *A2DP) streamState(st *a2dpStream, s avdtp.State) {
	a.mu.Lock()
	current := a.st == st
	a.mu.Unlock()
	if !current {
		return
	}
	switch s {
	case avdtp.StateStreaming:
		a.setState(A2DPStreaming, "")
	case avdtp.StateOpen:
		a.setState(A2DPReady, "")
	case avdtp.StateIdle:
		// The speaker closed the stream: drop to our own speaker and let
		// the next music reconnect.
		a.mu.Lock()
		if a.st == st {
			a.st = nil
		}
		a.mu.Unlock()
		go a.teardown(st)
		a.setState(A2DPOffline, "stream closed by the speaker")
	}
}

// inbound takes AVDTP and AVCTP channels a speaker opens to us. The first
// AVDTP channel on a link is signalling, the second the media transport
// (AVDTP 1.3 §5.4).
func (a *A2DP) inbound(l *ClassicLink, ch *L2Channel) {
	a.mu.Lock()
	if l.Addr != a.target {
		a.mu.Unlock()
		_ = ch.Close()
		return
	}
	st := a.st
	if st != nil && st.link != l {
		st = nil
	}
	switch {
	case ch.PSM == psmAVDTP && st == nil:
		st = &a2dpStream{link: l, sig: ch}
		a.st = st
		a.mu.Unlock()
		st.sess = a.newSession(st, ch, 0)
		a.setState(A2DPConnecting, "")
	case ch.PSM == psmAVDTP && st.media == nil:
		st.media = ch
		a.mu.Unlock()
		st.sess.MediaOpened()
	case ch.PSM == psmAVCTP && st != nil && st.rcCh == nil:
		a.mu.Unlock()
		a.attachRC(st, ch, true)
	default:
		a.mu.Unlock()
		_ = ch.Close()
	}
}

func (a *A2DP) linkDown(addr string) {
	a.mu.Lock()
	st := a.st
	if st == nil || st.link.Addr != addr {
		a.mu.Unlock()
		return
	}
	a.st = nil
	a.mu.Unlock()
	st.sess.Disconnected()
	if st.rc != nil {
		st.rc.Close()
	}
	a.setState(A2DPOffline, "speaker disconnected")
}

func (a *A2DP) teardown(st *a2dpStream) {
	if st.sess != nil && st.sess.State() != avdtp.StateIdle {
		_ = st.sess.Close()
	}
	if st.rc != nil {
		st.rc.Close()
	}
	for _, ch := range []*L2Channel{st.rcCh, st.media, st.sig} {
		if ch != nil {
			_ = ch.Close()
		}
	}
}

// encodeLoop turns music periods into SBC media packets for the current
// stream, starting it on the first music and suspending it after
// suspendAfter without any.
func (a *A2DP) encodeLoop() {
	var (
		cur      *a2dpStream
		enc      *sbc.Encoder
		cfg      sbc.Config
		pack     avdtp.Packetiser
		pcm      []int16 // samples not yet a whole frame
		frames   [][]byte
		frameBuf []byte
		lastPCM  time.Time
		held     bool
	)
	idle := time.NewTicker(time.Second)
	defer idle.Stop()
	release := func() {
		if held {
			a.c.Hold(false)
			held = false
		}
	}
	defer release()
	for {
		select {
		case <-a.stop:
			return
		case <-idle.C:
			a.mu.Lock()
			st := a.st
			a.mu.Unlock()
			streaming := st != nil && st.sess.State() == avdtp.StateStreaming
			if streaming && time.Since(lastPCM) > suspendAfter {
				if err := st.sess.Suspend(); err != nil {
					log.Printf("[a2dp] suspend: %v", err)
				}
				streaming = false
			}
			if !streaming {
				// Suspended, closed or gone: the scan may run again.
				release()
			}
		case p := <-a.periods:
			lastPCM = time.Now()
			a.mu.Lock()
			st := a.st
			a.mu.Unlock()
			if st == nil || st.media == nil {
				continue
			}
			if st != cur {
				c, ok := st.config()
				if !ok {
					continue
				}
				e, err := sbc.NewEncoder(c)
				if err != nil {
					log.Printf("[a2dp] encoder: %v", err)
					continue
				}
				cur, enc, cfg = st, e, c
				pack = avdtp.Packetiser{MTU: st.media.MTU(), SSRC: rand.Uint32(), Seq: uint16(rand.Uint32()), Timestamp: rand.Uint32()}
				pcm, frames = pcm[:0], frames[:0]
				frameBuf = make([]byte, cfg.FrameLength())
			}
			if st.sess.State() == avdtp.StateOpen {
				if !held {
					a.c.Hold(true) // the scan stays off while music streams
					held = true
				}
				if err := st.sess.Start(); err != nil {
					log.Printf("[a2dp] start: %v", err)
					continue
				}
			}
			if st.sess.State() != avdtp.StateStreaming {
				continue
			}
			pcm = appendPCM(pcm, p, cfg.Channels())
			spf := cfg.SamplesPerFrame() * cfg.Channels()
			for len(pcm) >= spf {
				n, err := enc.Encode(frameBuf, pcm[:spf])
				if err != nil {
					log.Printf("[a2dp] encode: %v", err)
					break
				}
				frames = append(frames, append([]byte(nil), frameBuf[:n]...))
				pcm = pcm[:copy(pcm, pcm[spf:])]
				if len(frames) >= avdtp.FramesPerPacket(pack.MTU, n) {
					frames = a.flush(st, &pack, frames, cfg.SamplesPerFrame())
				}
			}
		}
	}
}

func (a *A2DP) flush(st *a2dpStream, pack *avdtp.Packetiser, frames [][]byte, spf int) [][]byte {
	for len(frames) > 0 {
		pkt, n, err := pack.Pack(frames, spf)
		if err != nil {
			log.Printf("[a2dp] packetise: %v", err)
			return frames[:0]
		}
		if err := st.media.Send(pkt); err != nil {
			a.dropped.Add(uint64(n))
		}
		frames = frames[n:]
	}
	return frames[:0]
}

// appendPCM adds a stereo S16LE period to pcm as the encoder's interleaved
// samples: both channels, or their mean for a mono stream.
func appendPCM(pcm []int16, period []byte, channels int) []int16 {
	for i := 0; i+3 < len(period); i += 4 {
		l := int16(binary.LittleEndian.Uint16(period[i:]))
		r := int16(binary.LittleEndian.Uint16(period[i+2:]))
		if channels == 1 {
			pcm = append(pcm, int16((int32(l)+int32(r))>>1))
		} else {
			pcm = append(pcm, l, r)
		}
	}
	return pcm
}

// Close stops the encoder and drops the stream.
func (a *A2DP) Close() {
	close(a.stop)
	a.Select("", "")
}
