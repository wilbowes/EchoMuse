package firewall

import (
	"errors"
	"os/exec"
	"strings"
	"testing"
)

// fake is a filter holding a set of rules, one per tool, driven by the same
// -C/-I/-D verbs iptables takes.
type fake struct {
	rules   map[string]int // "tool rule" -> copies
	absent  map[string]bool
	calls   []string
	failIns bool
}

func (f *fake) run(bin string, args ...string) error {
	f.calls = append(f.calls, bin+" "+strings.Join(args, " "))
	if f.absent[bin] {
		return &exec.Error{Name: bin, Err: exec.ErrNotFound}
	}
	key := bin + " " + strings.Join(args[1:], " ")
	switch args[0] {
	case "-C":
		if f.rules[key] > 0 {
			return nil
		}
		return errors.New("no rule")
	case "-I":
		if f.failIns {
			return errors.New("insert failed")
		}
		f.rules[key]++
	case "-D":
		if f.rules[key] == 0 {
			return errors.New("no rule")
		}
		f.rules[key]--
	}
	return nil
}

func install(t *testing.T, f *fake) {
	f.rules = map[string]int{}
	old := run
	run = f.run
	t.Cleanup(func() { run = old })
}

const key4 = "iptables INPUT -p tcp --dport 8928 -j ACCEPT"

func TestOpenInsertsOnceAndCloseRemovesAll(t *testing.T) {
	f := &fake{}
	install(t, f)
	if err := Open(8928); err != nil {
		t.Fatal(err)
	}
	if err := Open(8928); err != nil {
		t.Fatal(err)
	}
	if f.rules[key4] != 1 {
		t.Fatalf("after two Opens: %d copies, want 1", f.rules[key4])
	}
	f.rules[key4]++ // one added by hand, as on 15LE
	Close(8928)
	if f.rules[key4] != 0 {
		t.Fatalf("after Close: %d copies, want 0", f.rules[key4])
	}
}

func TestMissingIp6tablesIsNotAnError(t *testing.T) {
	f := &fake{absent: map[string]bool{"ip6tables": true}}
	install(t, f)
	if err := Open(8928); err != nil {
		t.Fatalf("Open with no ip6tables: %v", err)
	}
	if f.rules[key4] != 1 {
		t.Fatal("iptables rule not added")
	}
	Close(8928) // must return, not loop
}

func TestInsertFailureIsReported(t *testing.T) {
	f := &fake{failIns: true}
	install(t, f)
	if Open(8928) == nil {
		t.Fatal("a failed insert must be reported: the player would be unreachable")
	}
}

func TestCloseWithNothingOpenIsQuiet(t *testing.T) {
	f := &fake{}
	install(t, f)
	Close(8928)
	if len(f.calls) != 2 {
		t.Fatalf("calls = %v, want one -D per tool", f.calls)
	}
}
