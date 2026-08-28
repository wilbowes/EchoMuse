//go:build crown

package speaker

import (
	"encoding/binary"
	"log"
	"net"
	"sync"
	"time"
)

// crownPlaybackSocket names the Linux ABSTRACT-namespace socket
// crown_launcher's PlaybackServer binds (PlaybackServer.SOCKET_NAME) — see
// device/crown_launcher/src/.../PlaybackServer.java. The leading "@" is
// Go's net package convention for dialing an abstract socket rather than a
// real path; Android's name is the same string with no prefix. Fixed, not
// discovered: both sides ship from the same repo and are always deployed
// together, so there's nothing to negotiate. Abstract, not filesystem —
// a first attempt at filesystem-namespace silently landed as abstract
// anyway (LocalServerSocket's String constructor always binds abstract;
// confirmed live via /proc/<pid>/net/unix), which turned out to simplify
// things: no stale socket file, and no permission bits to reason about at
// all for either of the daemon's launch paths (app-uid exec or root exec).
const crownPlaybackSocket = "@com.echomuse.crownlauncher/pcm"

// writeDeadline caps how long one period's write may block on the socket.
//
// It must NOT be tight against the ~32ms period rate: raw ALSA gave
// silenceLoop its pacing for free (Write blocked at hardware realtime
// rate — see the loop's own comment), but a socket write returns as soon
// as the OS socket buffer has room, so without AudioTrack's own playback
// backpressure this loop would flood the connection arbitrarily fast.
// That backpressure is real and expected in steady state — AudioTrack's
// blocking write on the Java side pauses draining the socket while it
// plays out its buffer — so the deadline exists only to catch a
// genuinely dead peer (service killed, socket abandoned), not to bound
// ordinary flow control. First value tried here (25ms) misread normal
// backpressure as a dead connection and thrashed reconnects continuously
// (confirmed live via the daemon log: connect/timeout/reconnect looping
// every few hundred ms) — measured and fixed 2026-08-26.
const writeDeadline = 500 * time.Millisecond

const reconnectBackoff = 250 * time.Millisecond

// socketPCM satisfies the same interface as *alsa.PCM (Write/Close) but
// feeds raw PCM to crown_launcher's AudioTrack-backed playback service
// over a Unix socket instead of opening /dev/snd directly — the fix for
// the mtk_pcm_I2S0dl1 freeze, see docs/echo-show-8-journal.md
// (2026-08-26 entries). Everything upstream
// (Mixer, audioStream, silenceLoop's pacing) is unchanged; this only
// replaces where the final mixed period goes.
//
// Write NEVER returns an error for a dropped/stalled connection — silently
// dropping a period (and logging at a throttled rate) is the deliberate
// choice over propagating an error, because pcm_speaker_crown.go's
// silenceLoop treats a Write error as fatal and exits the goroutine
// entirely. A socket hiccup is exactly the ordinary, expected condition
// this design exists to survive, not a reason to kill playback outright.
type socketPCM struct {
	path string

	mu          sync.Mutex
	conn        net.Conn
	lastAttempt time.Time

	dropCount   int
	lastDropLog time.Time

	// nextWrite paces writes to real time instead of relying on AudioTrack's
	// own backpressure to throttle us — this is load-bearing, not defensive
	// polish. Without it, connections died reliably after ~0.6-1.3s of
	// streaming with zero errors anywhere in the system log (isolated live
	// 2026-08-26: reproducible on both PERFORMANCE_MODE_LOW_LATENCY and
	// PERFORMANCE_MODE_NONE; bypassing AudioTrack.write() entirely ran clean
	// for 6+ seconds, proving the fault was in how the write was driven, not
	// AudioTrack/the HAL itself). Root cause: a socket write returns as soon
	// as the kernel buffer has room, so an unpaced writer produces whatever
	// backpressure allows — several periods in a burst, then a stall —
	// instead of one period every ~32ms the way raw ALSA's blocking Write
	// enforced for free. Explicit real-time pacing here fixed it outright:
	// 2+ minutes sustained, zero drops. Recorded at length because the
	// bursty-vs-silent-stall symptom looked exactly like a genuine platform
	// limitation until this was tried.
	nextWrite time.Time
}

func newSocketPCM(path string) *socketPCM {
	return &socketPCM{path: path}
}

func (s *socketPCM) ensureConn() net.Conn {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conn != nil {
		return s.conn
	}
	if time.Since(s.lastAttempt) < reconnectBackoff {
		return nil
	}
	s.lastAttempt = time.Now()
	c, err := net.DialTimeout("unix", s.path, writeDeadline)
	if err != nil {
		return nil
	}
	log.Println("[speaker] playback socket connected")
	s.conn = c
	return c
}

func (s *socketPCM) dropConn(reason error) {
	s.mu.Lock()
	if s.conn != nil {
		_ = s.conn.Close()
		s.conn = nil
	}
	s.mu.Unlock()
	log.Printf("[speaker] playback socket dropped: %v", reason)
}

// Write always reports success from the mixer loop's point of view — see
// the type comment for why. It counts and rate-limit-logs drops so a
// persistently dead socket is still visible in the logs without spamming
// one line per 32ms period.
func (s *socketPCM) Write(buf []byte) (int, error) {
	conn := s.ensureConn()
	if conn == nil {
		s.countDrop()
		return len(buf), nil
	}

	// Pace to real time — see the nextWrite field comment for why this is
	// load-bearing, not defensive polish.
	periodDur := time.Duration(len(buf)) * time.Second / (48000 * 4) // 4 bytes/frame, stereo S16LE
	now := time.Now()
	if s.nextWrite.IsZero() {
		s.nextWrite = now
	}
	if wait := s.nextWrite.Sub(now); wait > 0 {
		time.Sleep(wait)
	}
	s.nextWrite = s.nextWrite.Add(periodDur)
	if s.nextWrite.Before(now) {
		s.nextWrite = now // don't try to "catch up" after a long stall
	}

	_ = conn.SetWriteDeadline(time.Now().Add(writeDeadline))

	header := make([]byte, 4)
	binary.LittleEndian.PutUint32(header, uint32(len(buf)))
	if _, err := conn.Write(header); err != nil {
		s.dropConn(err)
		s.countDrop()
		return len(buf), nil
	}
	if _, err := conn.Write(buf); err != nil {
		s.dropConn(err)
		s.countDrop()
		return len(buf), nil
	}
	return len(buf), nil
}

func (s *socketPCM) countDrop() {
	s.dropCount++
	if time.Since(s.lastDropLog) >= 5*time.Second {
		log.Printf("[speaker] playback socket: %d periods dropped since last report", s.dropCount)
		s.dropCount = 0
		s.lastDropLog = time.Now()
	}
}

func (s *socketPCM) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conn != nil {
		return s.conn.Close()
	}
	return nil
}
