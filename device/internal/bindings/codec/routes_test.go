package codec

import (
	"errors"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/bindings/mixer"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// The tables are the board's and are pinned in pkg/board. These drive what
// the codec does with them, through a fake mixer: on a host the default
// backend fails every write.

type recordingBackend struct {
	sets [][]string
	fail map[string]bool
}

func (b *recordingBackend) Set(name string, values []string) error {
	if b.fail[name] {
		return errors.New("no such control")
	}
	b.sets = append(b.sets, append([]string{name}, values...))
	return nil
}

func (b *recordingBackend) Get(name string) (string, error) {
	return "", errors.New("no such control")
}

var (
	routes = []board.Ctl{{Name: "route A", Values: []string{"1"}}, {Name: "route B", Values: []string{"1"}}, {Name: "route C", Values: []string{"1"}}}
	gains  = []board.Ctl{{Name: "gain L", Values: []string{"0"}}, {Name: "gain pair", Values: []string{"24", "24"}}}
)

// Routes first, then gains, each control with every value it carries: a
// pair written with one value sets only its left channel.
func TestApplyWritesRoutesThenGains(t *testing.T) {
	b := &recordingBackend{}
	mixer.Use(b)
	apply(routes, gains)

	all := append(append([]board.Ctl{}, routes...), gains...)
	if len(b.sets) != len(all) {
		t.Fatalf("%d writes for %d controls", len(b.sets), len(all))
	}
	for i, c := range all {
		got := b.sets[i]
		if got[0] != c.Name || len(got)-1 != len(c.Values) {
			t.Errorf("write %d = %v, want %s %v", i, got, c.Name, c.Values)
			continue
		}
		for j, v := range c.Values {
			if got[j+1] != v {
				t.Errorf("write %d = %v, want %s %v", i, got, c.Name, c.Values)
			}
		}
	}
}

// A control that does not resolve is counted and does not stop the rest
// (#546: by NAME, an absent control is an error rather than a write to a
// neighbour). The control run, with nothing failing, writes everything, so
// the shortfall cannot be an artefact of the fake.
func TestApplyCarriesOnPastAFailure(t *testing.T) {
	b := &recordingBackend{fail: map[string]bool{"route A": true, "gain pair": true}}
	mixer.Use(b)
	apply(routes, gains)
	if want := len(routes) + len(gains) - 2; len(b.sets) != want {
		t.Fatalf("%d writes, want %d", len(b.sets), want)
	}
	if write(routes) != 1 || write(gains) != 1 {
		t.Error("each table must count its own failures")
	}

	ok := &recordingBackend{}
	mixer.Use(ok)
	apply(routes, gains)
	if want := len(routes) + len(gains); len(ok.sets) != want {
		t.Fatalf("control: %d writes, want %d", len(ok.sets), want)
	}
}

// sync.Once: DAPM decides what to power at stream open, so the routes go in
// before EITHER the mic or the speaker opens its PCM; both call this, and the
// second must not redo it.
func TestEnsureRoutesRunsOnlyOncePerProcess(t *testing.T) {
	b := &recordingBackend{}
	mixer.Use(b)
	EnsureRoutes()
	first := len(b.sets)
	EnsureRoutes()
	EnsureRoutes()
	if len(b.sets) != first {
		t.Fatalf("repeated calls wrote %d controls, want %d", len(b.sets), first)
	}
}
