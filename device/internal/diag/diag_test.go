package diag

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/board"
)

func fixture(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for p, body := range files {
		full := filepath.Join(root, p)
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func layout() *board.Layout {
	l := &board.Layout{
		Board:      board.Biscuit,
		Routes:     []board.Ctl{{Name: "DIF1 Switch", Values: []string{"On"}}},
		InputGains: []board.Ctl{{Name: "ADC Gain", Values: []string{"40", "40"}}},
		Notes:      []string{"keys: by name"},
	}
	l.Speaker.AmpSwitch = "Amp Switch"
	return l
}

func TestCollectReadsEachProbe(t *testing.T) {
	root := fixture(t, map[string]string{
		"/proc/idme/device_type_id": "A3S5BH2HU6VAYF\x00",
		"/proc/idme/alscal":         "ams_0_0 1 2 3",
		"/proc/idme/serial":         "G090SECRETSERIAL",
		"/proc/asound/cards":        " 0 [mtsndcard]: mt-snd-card",
		"/proc/asound/pcm":          "00-00: MultiMedia1 :  : playback 1",
		"/run/net.log":              "a\nb\n",
	})
	mixer := map[string]string{"DIF1 Switch": "Off", "ADC Gain": "40", "Amp Switch": "On"}
	s := Collect(Sources{
		Root:    root,
		Mixer:   func(n string) (string, error) { return mixer[n], nil },
		Kernel:  func() ([]byte, error) { return []byte("<6>[1.0] mt-snd-card: probe ok\n"), nil },
		WpaStat: func() (string, error) { return "wpa_state=COMPLETED\n", nil },
	}, layout())

	if s.Board["id"] != "biscuit" {
		t.Errorf("board = %v", s.Board)
	}
	if s.Idme["device_type_id"] != "A3S5BH2HU6VAYF" || s.Idme["alscal"] != "ams_0_0 1 2 3" {
		t.Errorf("idme = %v", s.Idme)
	}
	if _, ok := s.Idme["serial"]; ok {
		t.Error("idme carries the serial; only hardware fields are read")
	}
	if !strings.Contains(s.Files["/proc/asound/pcm"], "MultiMedia1") {
		t.Errorf("files = %v", s.Files)
	}
	// #546: the route reads back different from what the board writes.
	if len(s.Mixer) != 3 {
		t.Fatalf("mixer = %+v", s.Mixer)
	}
	for i, w := range []struct {
		name, want, got string
		match           *bool
	}{
		{"DIF1 Switch", "On", "Off", ptr(false)},
		{"ADC Gain", "40", "40", ptr(true)},
		{"Amp Switch", "", "On", nil},
	} {
		c := s.Mixer[i]
		if c.Name != w.name || c.Want != w.want || c.Got != w.got ||
			(c.Match == nil) != (w.match == nil) || (c.Match != nil && *c.Match != *w.match) {
			t.Errorf("mixer[%d] = %+v, want %+v", i, c, w)
		}
	}
	if len(s.Kernel) != 1 || s.WpaState == "" || len(s.NetLog) != 2 {
		t.Errorf("kernel %v wpa %q netlog %v", s.Kernel, s.WpaState, s.NetLog)
	}
	if s.Errors != nil {
		t.Errorf("errors = %v", s.Errors)
	}
}

func ptr(b bool) *bool { return &b }

// The boards write switches as "1"; tinyalsa reads them back as "On".
func TestSameReadsValuesAsTheMixerWritesThem(t *testing.T) {
	for _, c := range []struct {
		want, got string
		same      bool
	}{
		{"1", "On", true}, {"0", "Off", true}, {"On", "On", true},
		{"1", "Off", false}, {"24", "24", true}, {"24", "0", false},
		{"INT ADC", "INT ADC", true}, {"INT ADC", "On", false}, {"0", "0", true},
	} {
		if got := same(c.want, c.got); got != c.same {
			t.Errorf("same(%q, %q) = %v", c.want, c.got, got)
		}
	}
}

func TestAFailedProbeIsNamedNotEmpty(t *testing.T) {
	s := Collect(Sources{
		Root:    t.TempDir(),
		Mixer:   func(string) (string, error) { return "", errors.New("no control") },
		Kernel:  func() ([]byte, error) { return nil, errors.New("EPERM") },
		WpaStat: func() (string, error) { return "", errors.New("no socket") },
	}, layout())
	for _, k := range []string{"kernel", "wpaStatus", "/proc/asound/pcm"} {
		if s.Errors[k] == "" {
			t.Errorf("%s failed silently: %v", k, s.Errors)
		}
	}
	if s.Mixer[0].Err == "" || s.Mixer[0].Got != "" {
		t.Errorf("mixer error lost: %+v", s.Mixer[0])
	}
	// FireOS has no net.log; that is not a failure.
	if _, ok := s.Errors["/run/net.log"]; ok || s.NetLog != nil {
		t.Error("a missing net.log is reported")
	}
}

func TestKernelKeepsAudioAndDropsNetworkLines(t *testing.T) {
	log := strings.Join([]string{
		"[2.1] tas2770 3-004c: probe deferred",
		"[2.2] wlan: connected to SSID Bowes-Family-5G",
		"[2.3] asoc: mt8167-afe snd card registered",
		"[2.4] usb 1-1: new device",
		"[2.5] [wifi] audio coex ssid=Home",
	}, "\n")
	got := kernelFilter(log)
	if len(got) != 2 || !strings.Contains(got[0], "tas2770") || !strings.Contains(got[1], "asoc") {
		t.Errorf("kernel lines = %q", got)
	}
}

func TestOutputIsBounded(t *testing.T) {
	big := strings.Repeat("x", MaxText+100)
	root := fixture(t, map[string]string{"/proc/asound/cards": big})
	var lines []string
	for i := 0; i < KernelLines+50; i++ {
		lines = append(lines, "snd line")
	}
	s := Collect(Sources{Root: root, Kernel: func() ([]byte, error) {
		return []byte(strings.Join(lines, "\n")), nil
	}}, nil)
	if len(s.Files["/proc/asound/cards"]) != MaxText {
		t.Errorf("file not clipped: %d", len(s.Files["/proc/asound/cards"]))
	}
	if len(s.Kernel) != KernelLines {
		t.Errorf("kernel lines = %d", len(s.Kernel))
	}
	if s.Board["id"] != "unknown" || s.Mixer != nil {
		t.Errorf("nil layout: board %v mixer %v", s.Board, s.Mixer)
	}
}

// The controller never chooses what is read: no probe takes a path or a
// command from outside, and nothing here spawns a process.
func TestNothingIsSpawned(t *testing.T) {
	for _, f := range []string{"diag.go", "klog_linux.go"} {
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(b), "os/exec") {
			t.Errorf("%s imports os/exec", f)
		}
	}
}
