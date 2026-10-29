// Package diag collects the hardware and network state a support bundle asks
// for, so a reporter is not asked to paste it by hand.
//
// Every probe is a fixed read: files, a syscall, and wpa_cli with constant
// arguments. Nothing the controller sends reaches a path or a command. The
// output goes to the controller raw, over the device's own authenticated
// link (it already carries the SSID in stats), and the controller redacts it
// before anything is written to a bundle (em_support.redact_snapshot).
//
// Asked for by hand, before this: mixer state (#546, #630), /proc/asound/pcm
// (#527), idme device_type_id and alscal (#541, #90), audio driver probe lines
// (#826), wpa_cli status (#536) and /run/net.log (#767).
package diag

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/wilbowes/EchoMuse/pkg/board"
)

// MaxText caps any one probe's text. The bundle has a budget, and a runaway
// file must not crowd out the rest.
const MaxText = 32 << 10

// KernelLines caps the kernel log lines kept, newest last.
const KernelLines = 200

// NetLogLines matches the wizard's tail of the same log.
const NetLogLines = 200

// idme fields that describe the hardware. Not the serial, which the bundle
// carries already, and not the MACs.
var idmeFields = []string{"device_type_id", "board_id", "product_name", "productid", "alscal"}

// Kernel log lines from the audio drivers and deferred probes, which is what
// #826 needed. WiFi driver lines are left on the device: the MTK driver logs
// network names in prose.
var kernelAudio = regexp.MustCompile(`(?i)snd|asoc|alsa|aic3|tas2|tlv320|codec|i2s|tdm|mtk-afe|afe|spi|defer|audio`)
var kernelNetwork = regexp.MustCompile(`(?i)ssid|wlan|wifi|wpa|80211`)

// Sources are the reads that need the device. Each is nil in a test that
// does not exercise it.
type Sources struct {
	Root    string // "" on a device, a fixture dir in tests
	Mixer   func(name string) (string, error)
	Kernel  func() ([]byte, error) // the kernel ring buffer
	WpaStat func() (string, error) // wpa_cli status
}

// Control is one mixer control this board writes, read back.
type Control struct {
	Name string `json:"name"`
	Want string `json:"want,omitempty"` // what the board writes; "" for state-dependent controls
	Got  string `json:"got,omitempty"`
	Err  string `json:"err,omitempty"`
	// Match is whether Got is the value Want writes, by the mixer's own
	// reading of values: a switch written "1" reads back "On". Nil when there
	// is nothing to compare.
	Match *bool `json:"match,omitempty"`
}

// Snapshot is the reply's body. A probe that failed is named in Errors and
// absent elsewhere, so missing is never mistaken for empty.
type Snapshot struct {
	Board    map[string]any    `json:"board"`
	Idme     map[string]string `json:"idme"`
	Mixer    []Control         `json:"mixer"`
	Files    map[string]string `json:"files"`
	Kernel   []string          `json:"kernel"`
	WpaState string            `json:"wpaStatus,omitempty"`
	NetLog   []string          `json:"netLog,omitempty"`
	Errors   map[string]string `json:"errors,omitempty"`
}

// Collect runs every probe. It never fails as a whole.
func Collect(src Sources, layout *board.Layout) Snapshot {
	s := Snapshot{Idme: map[string]string{}, Files: map[string]string{}, Errors: map[string]string{}}
	s.Board = boardInfo(layout)
	for _, f := range idmeFields {
		if v, err := readText(src.Root, "/proc/idme/"+f); err == nil {
			s.Idme[f] = strings.TrimSpace(strings.TrimRight(v, "\x00"))
		}
	}
	for _, f := range []string{"/proc/asound/cards", "/proc/asound/pcm"} {
		v, err := readText(src.Root, f)
		if err != nil {
			s.Errors[f] = err.Error()
			continue
		}
		s.Files[f] = v
	}
	if src.Mixer != nil && layout != nil {
		s.Mixer = readMixer(src.Mixer, layout)
	}
	if src.Kernel != nil {
		if b, err := src.Kernel(); err != nil {
			s.Errors["kernel"] = err.Error()
		} else {
			s.Kernel = kernelFilter(string(b))
		}
	}
	if src.WpaStat != nil {
		if v, err := src.WpaStat(); err != nil {
			s.Errors["wpaStatus"] = err.Error()
		} else {
			s.WpaState = clip(v)
		}
	}
	// emOS only: init writes it. Its absence on FireOS is not an error.
	if v, err := readText(src.Root, "/run/net.log"); err == nil {
		s.NetLog = tail(strings.Split(strings.TrimRight(v, "\n"), "\n"), NetLogLines)
	}
	if len(s.Errors) == 0 {
		s.Errors = nil
	}
	return s
}

func boardInfo(l *board.Layout) map[string]any {
	if l == nil {
		return map[string]any{"id": "unknown"}
	}
	return map[string]any{
		"id":       board.IDOf(l.Board),
		"notes":    l.Notes,
		"problems": l.Problems,
	}
}

// readMixer reads back every control the board writes at start-up, beside
// the value it writes, so a route that did not take (#546: a valid write to
// the wrong control) shows as a mismatch. Only the first value of a
// multi-value control is read.
func readMixer(get func(string) (string, error), l *board.Layout) []Control {
	var out []Control
	seen := map[string]bool{}
	add := func(name, want string) {
		if name == "" || seen[name] {
			return
		}
		seen[name] = true
		c := Control{Name: name, Want: want}
		if v, err := get(name); err != nil {
			c.Err = err.Error()
		} else {
			c.Got = v
			if want != "" {
				m := same(want, v)
				c.Match = &m
			}
		}
		out = append(out, c)
	}
	for _, group := range [][]board.Ctl{l.Routes, l.InputGains, l.Speaker.Setup} {
		for _, c := range group {
			want := ""
			if len(c.Values) > 0 {
				want = c.Values[0]
			}
			add(c.Name, want)
		}
	}
	// Written at run time, so there is no fixed value to compare with.
	add(l.Speaker.AmpSwitch, "")
	add(l.Speaker.AmpMute, "")
	add(l.Speaker.Volume.Control, "")
	return out
}

// same compares as mixer.Set writes: switches accept 1/On and 0/Off, the rest
// are enum names or integers, which read back as written.
func same(want, got string) bool {
	if want == got {
		return true
	}
	w, wok := switchValue(want)
	g, gok := switchValue(got)
	return wok && gok && w == g
}

func switchValue(s string) (bool, bool) {
	switch s {
	case "1", "On", "on":
		return true, true
	case "0", "Off", "off":
		return false, true
	}
	return false, false
}

func kernelFilter(log string) []string {
	var keep []string
	for _, ln := range strings.Split(log, "\n") {
		if kernelAudio.MatchString(ln) && !kernelNetwork.MatchString(ln) {
			keep = append(keep, strings.TrimRight(ln, "\r"))
		}
	}
	return tail(keep, KernelLines)
}

func readText(root, path string) (string, error) {
	b, err := os.ReadFile(filepath.Join(root, path))
	if err != nil {
		return "", err
	}
	return clip(string(b)), nil
}

func clip(s string) string {
	if len(s) > MaxText {
		return s[len(s)-MaxText:]
	}
	return s
}

func tail(lines []string, n int) []string {
	if len(lines) > n {
		return lines[len(lines)-n:]
	}
	return lines
}
