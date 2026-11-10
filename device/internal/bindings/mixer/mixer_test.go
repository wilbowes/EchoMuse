package mixer

import (
	"errors"
	"reflect"
	"testing"
)

type fake struct {
	sets   [][]string
	values map[string]string
	fail   bool
}

func (f *fake) Set(name string, v []string) error {
	if f.fail {
		return errors.New("no such control")
	}
	f.sets = append(f.sets, append([]string{name}, v...))
	return nil
}

func (f *fake) Get(name string) (string, error) {
	v, ok := f.values[name]
	if !ok {
		return "", errors.New("no such control")
	}
	return v, nil
}

func TestSetPassesNameAndValues(t *testing.T) {
	f := &fake{}
	Use(f)
	defer Use(unavailable{})
	if err := Set("PCM Playback Volume", "100", "100"); err != nil {
		t.Fatal(err)
	}
	want := [][]string{{"PCM Playback Volume", "100", "100"}}
	if !reflect.DeepEqual(f.sets, want) {
		t.Fatalf("got %v", f.sets)
	}
	if err := Set("Ext_Speaker_Amp_Switch"); err == nil {
		t.Fatal("a write with no value must be refused")
	}
}

func TestFailuresAreReturnedEveryTime(t *testing.T) {
	Use(&fake{fail: true})
	defer Use(unavailable{})
	for i := 0; i < 3; i++ {
		if Set("Ext_Speaker_Amp_Switch", "On") == nil {
			t.Fatalf("attempt %d: error swallowed", i)
		}
	}
}

func TestHostBuildHasNoBackend(t *testing.T) {
	Use(unavailable{})
	if Set("Ext_Speaker_Amp_Switch", "On") == nil {
		t.Fatal("a build with no mixer must not report success")
	}
	if _, err := Get("Ext_Speaker_Amp_Switch"); err == nil {
		t.Fatal("a build with no mixer must not report a value")
	}
}

func TestSpread(t *testing.T) {
	got, err := spread("x", []string{"7"}, 2)
	if err != nil || !reflect.DeepEqual(got, []string{"7", "7"}) {
		t.Fatalf("one value, two channels: %v %v", got, err)
	}
	if got, err = spread("x", []string{"1", "2"}, 2); err != nil || got[1] != "2" {
		t.Fatalf("one per channel: %v %v", got, err)
	}
	if _, err = spread("x", []string{"1", "2"}, 1); err == nil {
		t.Fatal("too many values must be refused")
	}
	if _, err = spread("x", []string{"1"}, 0); err == nil {
		t.Fatal("a control with no values must be refused")
	}
}

func TestBoolValue(t *testing.T) {
	for s, want := range map[string]int{"1": 1, "On": 1, "on": 1, "0": 0, "Off": 0, "off": 0} {
		if v, ok := boolValue(s); !ok || v != want {
			t.Errorf("%q = %d,%v", s, v, ok)
		}
	}
	for _, s := range []string{"", "2", "true", "ON "} {
		if _, ok := boolValue(s); ok {
			t.Errorf("%q accepted", s)
		}
	}
}

func TestByteValues(t *testing.T) {
	if b, err := byteValues("f", []string{"0", "128", "255"}, 3); err != nil || string(b) != "\x00\x80\xff" {
		t.Fatalf("got %v, %v", b, err)
	}
	for _, bad := range [][]string{
		{"7"},                // one value is never spread over a filter
		{"1", "2"},           // short
		{"1", "2", "3", "4"}, // long
		{"1", "256", "3"},    // out of range
		{"1", "-1", "3"},
		{"1", "0x10", "3"},
		{"1", "", "3"},
	} {
		if _, err := byteValues("f", bad, 3); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}
