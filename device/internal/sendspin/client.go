package sendspin

import (
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
	"github.com/grandcat/zeroconf"
)

// Port and path are the spec's recommendations, which is also where Music
// Assistant looks if a user types the address by hand.
const (
	DefaultPort = 8928
	Path        = "/sendspin"
	serviceType = "_sendspin._tcp"
	maxSessions = 4
)

// Config is what the device supplies. Everything else is Sendspin's own.
type Config struct {
	StorePath string // persisted identity, pairing records and player settings
	Name      string // shown in Music Assistant
	Instance  string // mDNS instance name; unique on the LAN
	Port      int
	Product   string
	Version   string
	// Unpaired lets a server play without pairing, once its operator has
	// approved the device. Off by default: anyone on the LAN can claim to
	// be a server, and pairing is one paste of the device's token.
	Unpaired bool
}

// Client is the device's Sendspin player. Servers connect to it (the spec's
// recommended direction, and the one Music Assistant uses): it advertises
// itself over mDNS and listens.
type Client struct {
	cfg    Config
	store  *store
	player *player
	oc     atomic.Pointer[OutputClock] // made and fed by the speaker goroutine (Fill); read for diagnostics

	mu         sync.Mutex
	admitted   *session
	rankOf     map[*session]int
	sessions   map[*session]bool
	extBusy    bool
	unpaired   bool
	stereo     bool // a plug is in the jack: stereo is the preferred format
	onChange   func()
	onSettings func(playerSettings)
	// onVolume, when set, makes the server's volume the DEVICE's volume:
	// commands go to it rather than to a gain on synced music alone, and the
	// device reports its level back through SetVolume. One volume, whoever
	// moves it (Wil, 2026-09-30).
	onVolume func(v int)

	srv  *http.Server
	mdns *zeroconf.Server
}

// New loads (or creates) the device's Sendspin identity. It does not listen
// until Start.
func New(cfg Config) (*Client, error) {
	if cfg.Port == 0 {
		cfg.Port = DefaultPort
	}
	st, err := openStore(cfg.StorePath)
	if err != nil {
		return nil, fmt.Errorf("sendspin state %s: %w", cfg.StorePath, err)
	}
	c := &Client{
		cfg:      cfg,
		store:    st,
		rankOf:   map[*session]int{},
		sessions: map[*session]bool{},
		unpaired: cfg.Unpaired,
	}
	c.player = newPlayer(newTimeFilter())
	set := st.settings()
	c.player.setGain(set.Volume, set.Muted) // until OnVolume hands it to the device
	c.player.setDelay(set.StaticDelayMs)
	return c, nil
}

// ClientID is the device's Sendspin identity (its public key). Not secret.
func (c *Client) ClientID() string { return c.store.id.clientID() }

// PairingToken is what the user pastes into Music Assistant to pair. It
// carries the pairing PSK, so it is a secret: never log it.
func (c *Client) PairingToken() string { return c.store.token() }

// OnChange is called (on its own goroutine) whenever the status moves.
func (c *Client) OnChange(f func()) {
	c.mu.Lock()
	c.onChange = f
	c.mu.Unlock()
}

func (c *Client) changed() {
	c.mu.Lock()
	f := c.onChange
	c.mu.Unlock()
	if f != nil {
		go f()
	}
}

// Start listens and advertises. It returns once both are up.
func (c *Client) Start() error {
	ln, err := net.Listen("tcp", ":"+strconv.Itoa(c.cfg.Port))
	if err != nil {
		return err
	}
	mux := http.NewServeMux()
	up := websocket.Upgrader{
		// Not a browser endpoint: the Noise handshake authenticates, and a
		// server has no Origin worth checking.
		CheckOrigin: func(*http.Request) bool { return true },
	}
	mux.HandleFunc(Path, func(w http.ResponseWriter, r *http.Request) {
		// A cap on open connections: anything on the LAN can open one and
		// hold it for the 30s handshake window, and each costs a goroutine
		// and buffers. The spec lets a client refuse past a cap. Four covers
		// a playback server, a pairing one and a displacement in flight.
		c.mu.Lock()
		full := len(c.sessions) >= maxSessions
		c.mu.Unlock()
		if full {
			http.Error(w, "busy", http.StatusServiceUnavailable)
			return
		}
		ws, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		s := newSession(c, ws)
		c.mu.Lock()
		c.sessions[s] = true
		c.mu.Unlock()
		go s.run()
	})
	c.srv = &http.Server{Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go c.srv.Serve(ln)

	c.mdns, err = zeroconf.Register(c.cfg.Instance, serviceType, "local.", c.cfg.Port,
		[]string{"path=" + Path, "name=" + c.cfg.Name}, nil)
	if err != nil {
		c.srv.Close()
		return fmt.Errorf("mDNS: %w", err)
	}
	log.Printf("[sendspin] player %q listening on :%d%s", c.cfg.Name, c.cfg.Port, Path)
	return nil
}

// Stop says goodbye to every server and stops listening.
func (c *Client) Stop(reason string) {
	c.mu.Lock()
	var all []*session
	for s := range c.sessions {
		all = append(all, s)
	}
	c.mu.Unlock()
	for _, s := range all {
		s.sendJSON("client/goodbye", clientGoodbye{Reason: reason}, true)
		s.ws.Close()
	}
	if c.mdns != nil {
		c.mdns.Shutdown()
	}
	if c.srv != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		c.srv.Shutdown(ctx)
		cancel()
	}
	c.player.clear()
}

// SetUnpaired changes whether servers may play without pairing. A change
// restarts every connection (goodbye "restart"), since client/hello
// advertised the old value and is sent once per connection.
func (c *Client) SetUnpaired(on bool) {
	c.mu.Lock()
	if c.unpaired == on {
		c.mu.Unlock()
		return
	}
	c.unpaired = on
	var all []*session
	for s := range c.sessions {
		all = append(all, s)
	}
	c.mu.Unlock()
	for _, s := range all {
		s.sendJSON("client/goodbye", clientGoodbye{Reason: "restart"}, true)
		s.ws.Close()
	}
}

func (c *Client) unpairedAccess() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.unpaired
}

// SetStereo is set while a plug is in the jack (#273). The internal speaker
// is mono, so mono is preferred and the wire carries half the bytes; with a
// plug in, the second channel is real. A change is told to every connected
// server with stream/request-format, which renegotiates a stream mid-play;
// a new connection reads it from client/hello.
func (c *Client) SetStereo(on bool) {
	c.mu.Lock()
	if c.stereo == on {
		c.mu.Unlock()
		return
	}
	c.stereo = on
	var all []*session
	for s := range c.sessions {
		all = append(all, s)
	}
	c.mu.Unlock()
	f := formats(on)[0]
	log.Printf("[sendspin] preferred format: %dch", f.Channels)
	for _, s := range all {
		if s.playerRole() {
			s.send("stream/request-format", streamRequestFormat{Player: &f})
		}
	}
}

func (c *Client) stereoPreferred() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.stereo
}

// SetExternal is set while something other than Sendspin owns the music
// plane — Home Assistant's own media player. Decided 2026-08-22 (Wil): HA
// wins, since it is the user's direct request. The device reports itself
// unavailable, which moves it out of its group and stops the stream, and
// the server does not rejoin it afterwards: someone restarts the group.
func (c *Client) SetExternal(busy bool) {
	c.mu.Lock()
	if c.extBusy == busy {
		c.mu.Unlock()
		return
	}
	c.extBusy = busy
	s := c.admitted
	c.mu.Unlock()
	if busy {
		c.player.clear()
	}
	if s != nil {
		s.sendState()
	}
	c.changed()
}

func (c *Client) external() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.extBusy
}

// OnSettings is told when a server changes volume, mute or delay, so the
// device can reflect it (they are also persisted here).
func (c *Client) OnSettings(f func(volume int, muted bool, delayMs int)) {
	c.mu.Lock()
	c.onSettings = func(p playerSettings) { f(p.Volume, p.Muted, p.StaticDelayMs) }
	c.mu.Unlock()
}

// OnVolume hands volume commands to the device (see onVolume). Set before
// Start.
func (c *Client) OnVolume(f func(v int)) {
	c.mu.Lock()
	c.onVolume = f
	c.mu.Unlock()
	set := c.store.settings()
	c.player.setGain(c.gainVolume(set.Volume), set.Muted)
}

// SetVolume reports the device's volume (0-100) when it changes by any
// route: its buttons, Home Assistant, or a command this client passed on.
// Sent to the server only when it differs from what it last heard.
func (c *Client) SetVolume(v int) {
	set := c.store.settings()
	if set.Volume == v {
		return
	}
	set.Volume = v
	c.store.setSettings(set)
	c.mu.Lock()
	s := c.admitted
	c.mu.Unlock()
	if s != nil {
		s.sendState()
	}
	c.changed()
}

// gainVolume is the volume the player itself applies: none when the device
// owns the volume, since the device applies it to everything it plays.
func (c *Client) gainVolume(v int) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.onVolume != nil {
		return 100
	}
	return v
}

func (c *Client) applySettings(p playerSettings) {
	prev := c.store.settings()
	c.store.setSettings(p)
	c.mu.Lock()
	vf := c.onVolume
	c.mu.Unlock()
	if vf != nil && p.Volume != prev.Volume {
		vf(p.Volume)
	}
	c.player.setGain(c.gainVolume(p.Volume), p.Muted)
	c.player.setDelay(p.StaticDelayMs)
	c.mu.Lock()
	f := c.onSettings
	c.mu.Unlock()
	if f != nil {
		f(p)
	}
	c.changed()
}

// Fill is the speaker's pull: one stereo period whose first frame reaches the
// DAC at playAt, as MEASURED — it is smoothed here (OutputClock), so the
// caller passes its raw reading, and how uncertain that reading is. A reading
// the scheduler interrupted is not learned from: the tracker coasts on its
// prediction for that period. False when there is nothing to play then.
// Called from one goroutine only.
func (c *Client) Fill(out []byte, playAt time.Time, uncertain time.Duration) bool {
	oc := c.oc.Load()
	if oc == nil {
		oc = NewOutputClock(time.Duration(len(out)/4) * time.Second / outRate)
		c.oc.Store(oc)
	}
	var at time.Time
	if uncertain > outClockTrust {
		at = oc.Coast(playAt)
	} else {
		at = oc.Observe(playAt)
	}
	return c.player.Fill(out, at)
}

// Active reports whether Sendspin has audio queued: the speaker's cue that
// the music plane is taken.
func (c *Client) Active() bool { return c.player.active() }

// Buffered is how much audio is queued and not yet played.
func (c *Client) Buffered() time.Duration { return c.player.buffered() }

// OutDiag takes the DAC position tracker's summary since the last call.
func (c *Client) OutDiag() OutDiag {
	if oc := c.oc.Load(); oc != nil {
		return oc.TakeDiag()
	}
	return OutDiag{}
}

// SyncDiag takes the connected server's time-exchange summary since the last
// call; false with no server connected.
func (c *Client) SyncDiag() (SyncDiag, bool) {
	c.mu.Lock()
	s := c.admitted
	c.mu.Unlock()
	if s == nil {
		return SyncDiag{}, false
	}
	return s.filter.takeDiag(), true
}

func (c *Client) hello() clientHello {
	return clientHello{
		Name:           c.cfg.Name,
		SupportedRoles: []string{rolePlayer},
		DeviceInfo: &deviceInfo{
			ProductName:     c.cfg.Product,
			Manufacturer:    "EchoMuse",
			SoftwareVersion: c.cfg.Version,
		},
		PlayerSupport: &playerSupport{
			SupportedFormats:  formats(c.stereoPreferred()),
			BufferCapacity:    bufferCapacity,
			SupportedCommands: []string{"volume", "mute"},
		},
		SupportedPairMethods: []pairMethod{{Method: methodPSK}},
		UnpairedAccess:       unpairedAccess{Enabled: c.unpairedAccess()},
	}
}

// bufferCapacity caps the compressed bytes a server may have outstanding.
// Sized as 6s of the same audio as stereo PCM, so FLAC's ~2:1 buys more time
// rather than more memory: roughly 12s of stereo decoded, ~2.3MB. One figure
// for both formats, because hello is sent once per connection and the format
// can change after it.
const bufferCapacity = 6 * outRate * 2 * outBits / 8

// ── admission ──────────────────────────────────────────────────────────────
//
// One admitted connection at a time, ranked by what it declares: playback
// beats pairing beats nothing. An equal or higher newcomer displaces the
// holder, except that a pairing in progress is never displaced, and between
// two that declare nothing the last server to have played wins.

func (c *Client) admit(s *session, r int) bool {
	c.mu.Lock()
	cur := c.admitted
	curRank := c.rankOf[cur]
	ok := true
	switch {
	case cur == nil || cur == s:
	case curRank == 1 && cur.stagedPSK != nil:
		ok = false
	case r == 0 && curRank == 0:
		last := c.store.lastPlayback()
		ok = s.serverID == last && cur.serverID != last
	default:
		ok = r >= curRank
	}
	if ok {
		c.admitted = s
		c.rankOf[s] = r
	}
	c.mu.Unlock()
	if ok {
		c.player.setFilter(s.filter)
	}
	if ok && cur != nil && cur != s {
		cur.goodbye("another_server")
		cur.ws.Close()
		c.player.clear()
	}
	return ok
}

func (c *Client) rerank(s *session, r int) {
	c.mu.Lock()
	c.rankOf[s] = r
	c.mu.Unlock()
}

func (c *Client) isAdmitted(s *session) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.admitted == s
}

// inUse says whether a pairing record backs a live connection, so eviction
// never removes one.
func (c *Client) inUse(serverID string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for s := range c.sessions {
		if s.serverID == serverID {
			return true
		}
	}
	return false
}

func (c *Client) release(s *session) {
	c.mu.Lock()
	delete(c.sessions, s)
	delete(c.rankOf, s)
	was := c.admitted == s
	if was {
		c.admitted = nil
	}
	c.mu.Unlock()
	if was {
		c.player.clear()
		log.Printf("[sendspin] %q disconnected", s.name())
		c.changed()
	}
}

// ── status ─────────────────────────────────────────────────────────────────

// Status is what the controller shows. No secrets.
type Status struct {
	ClientID   string      `json:"clientId"`
	State      string      `json:"state"` // listening | connected | playing | busy
	Server     string      `json:"server,omitempty"`
	Paired     bool        `json:"paired"`
	PairedWith int         `json:"pairedWith"`
	Group      string      `json:"group,omitempty"`
	Synced     bool        `json:"synced"`
	SyncErrUs  int64       `json:"syncErrUs,omitempty"`
	BufferedMs int64       `json:"bufferedMs"`
	Volume     int         `json:"volume"`
	Muted      bool        `json:"muted"`
	DelayMs    int         `json:"delayMs"`
	Unpaired   bool        `json:"unpairedAccess"`
	Player     playerStats `json:"player"`
}

func (c *Client) Status() Status {
	set := c.store.settings()
	st := Status{
		ClientID:   c.ClientID(),
		State:      "listening",
		PairedWith: len(c.store.paired()),
		Volume:     set.Volume,
		Muted:      set.Muted,
		DelayMs:    set.StaticDelayMs,
		Unpaired:   c.unpairedAccess(),
		BufferedMs: c.player.buffered().Milliseconds(),
		Player:     c.player.stats(),
	}
	c.mu.Lock()
	s, busy := c.admitted, c.extBusy
	c.mu.Unlock()
	if s != nil {
		st.State = "connected"
		st.Synced = s.filter.synchronized()
		if st.Synced {
			st.SyncErrUs = s.filter.errorUs()
		}
		s.mu.Lock()
		st.Server = s.serverName
		st.Paired = s.psk.cat == catLongTerm
		st.Group = s.group.GroupName
		if time.Since(s.lastAudio) < 2*time.Second {
			st.State = "playing"
		}
		s.mu.Unlock()
	}
	if busy {
		st.State = "busy"
	}
	return st
}
