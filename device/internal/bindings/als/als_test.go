package als

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The threshold policy decides what reaches Home Assistant promptly and what
// waits up to 30s for the stats tick. Too sensitive and a flickering lamp
// floods the control plane; too coarse and "someone turned a light on" is the
// thing it misses.
func TestSignificant(t *testing.T) {
	cases := []struct {
		name          string
		baseline, now int
		want          bool
	}{
		// Measured noise on a still room: 309/311/313/308/312 on consecutive
		// reads, about ±1.5%. None of it may report.
		{"still room drift up", 309, 313, false},
		{"still room drift down", 313, 308, false},

		// The case this exists for.
		{"lamp switched on", 40, 300, true},
		{"lamp switched off", 300, 40, true},

		// Hand over the sensor, measured 309 -> 0.
		{"covered", 309, 0, true},
		{"uncovered", 0, 308, true},

		// Near darkness must not produce infinite ratios: a 2 lux wobble in a
		// dark room is not a room lighting up.
		{"tiny change near zero", 0, 2, false},
		{"tiny change near zero, down", 3, 0, false},

		// Big RELATIVE change but small absolute — still noise-ish.
		{"5 to 9 lux", 5, 9, false},

		// Daylight: 50 lux is invisible against 20000 and must not report,
		// which an absolute threshold would get wrong.
		{"daylight jitter", 20000, 20050, false},
		{"cloud clears", 8000, 20000, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := Significant(c.baseline, c.now); got != c.want {
				t.Fatalf("Significant(%d, %d) = %v, want %v",
					c.baseline, c.now, got, c.want)
			}
		})
	}
}

// Symmetry matters: a light going off should be as reportable as one coming
// on. Comparing against the baseline rather than the new value is what makes
// that true, and it is easy to get backwards.
func TestSignificantIsSymmetricEnough(t *testing.T) {
	if !Significant(300, 40) || !Significant(40, 300) {
		t.Fatal("a lamp must be reportable in both directions")
	}
}

// ── Status reporting (issue #90) ─────────────────────────────────────────────
//
// Two users' Dots reported no ambient light sensor and there was no way to
// tell whether the chip was absent or the driver had not bound, because the
// answer was only ever written to a log file on the device. These pin the
// three verdicts against a fake i2c bus.

// fakeBus builds an i2c tree and points the package at it. Returns the root.
// Also resets the package's cached state, which otherwise leaks between tests
// and makes the second one assert against the first one's scan.
func fakeBus(t *testing.T, devices map[string]bool) {
	t.Helper()
	root := t.TempDir()
	for name, withAttr := range devices {
		dir := filepath.Join(root, name)
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "name"), []byte(name+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		if withAttr {
			if err := os.WriteFile(filepath.Join(dir, "als_lux"), []byte("42\n"), 0o644); err != nil {
				t.Fatal(err)
			}
		}
	}

	mu.Lock()
	path, lastScan, reported = "", time.Time{}, false
	status = Status{Code: StatusUnknown}
	mu.Unlock()

	old, oldIIO, oldCal := i2cGlob, iioGlob, alscalPath
	i2cGlob = filepath.Join(root, "*", "name")
	iioGlob = filepath.Join(t.TempDir(), "*", "name") // empty unless fakeIIO
	alscalPath = filepath.Join(root, "no-alscal")
	t.Cleanup(func() {
		i2cGlob, iioGlob, alscalPath = old, oldIIO, oldCal
		mu.Lock()
		path, lastScan, reported = "", time.Time{}, false
		status = Status{Code: StatusUnknown}
		mu.Unlock()
	})
}

func TestStatusOK(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2540": true, "tsl2584tsv": false})
	got := Report()
	if got.Code != StatusOK {
		t.Fatalf("code = %q, want %q (detail %q)", got.Code, StatusOK, got.Detail)
	}
	if got.Path == "" {
		t.Fatal("a resolved sensor must report where it was found")
	}
	if !Present() {
		t.Fatal("Present() must be true when the sensor resolves")
	}
}

// Seen must list the WHOLE bus even when the sensor is found early.
//
// The first version returned as soon as it matched, so a working device
// reported only the names sorting before tsl2540 — on real hardware that
// dropped is31fl3236, tlv320aic32x4 and bq24297. Comparing a healthy bus
// against a broken one is what this field is for, so a truncated list from
// the healthy side defeats the purpose. The fixtures missed it because none
// had a device sorting after the match; `zz_after` is here to guarantee one
// always does.
func TestSeenListsWholeBusWhenSensorFound(t *testing.T) {
	fakeBus(t, map[string]bool{
		"aa_before": false,
		"tsl2540":   true,
		"zz_after":  false,
	})
	got := Report()
	if got.Code != StatusOK {
		t.Fatalf("code = %q, want %q", got.Code, StatusOK)
	}
	if len(got.Seen) != 3 {
		t.Fatalf("Seen = %v, want all three bus devices", got.Seen)
	}
	var sawAfter bool
	for _, s := range got.Seen {
		if s == "zz_after" {
			sawAfter = true
		}
	}
	if !sawAfter {
		t.Fatalf("Seen = %v, missing the device that sorts after the sensor", got.Seen)
	}
}

// The hypothesis for #90: these units carry the second ALS, which is present
// on working devices too but has no usable driver, and not the tsl2540.
func TestStatusNoChip(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2584tsv": false, "tlv320aic3101": false})
	got := Report()
	if got.Code != StatusNoChip {
		t.Fatalf("code = %q, want %q", got.Code, StatusNoChip)
	}
	// Seen is what makes the answer verifiable rather than merely asserted,
	// and identifies an unfamiliar revision the first time one appears.
	if len(got.Seen) != 2 {
		t.Fatalf("Seen = %v, want both bus devices", got.Seen)
	}
	if Present() {
		t.Fatal("Present() must be false with no tsl2540")
	}
}

// Distinct from no_chip because the fixes are opposite: this one is ours.
func TestStatusNoAttribute(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2540": false})
	got := Report()
	if got.Code != StatusNoAttribute {
		t.Fatalf("code = %q, want %q", got.Code, StatusNoAttribute)
	}
	if got.Detail == "" {
		t.Fatal("a failure must carry a detail a human can read in a bundle")
	}
}

// The absence LOG is deliberately once-only; the status must not be, or a
// device whose bus changed would keep reporting its first answer forever.
func TestStatusRefreshesAcrossScans(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2584tsv": false})
	if got := Report(); got.Code != StatusNoChip {
		t.Fatalf("first scan: code = %q, want %q", got.Code, StatusNoChip)
	}
	mu.Lock()
	lastScan = time.Time{} // allow an immediate re-scan
	mu.Unlock()
	if got := Report(); got.Code != StatusNoChip {
		t.Fatalf("second scan: code = %q, want %q", got.Code, StatusNoChip)
	}
}

// fakeIIO puts a tsl2584tsv on the IIO bus, as the G090LF096 batch has it
// (#90), with the idme calibration string `alscal` ("" for none).
func fakeIIO(t *testing.T, alscal string) string {
	t.Helper()
	root := t.TempDir()
	dir := filepath.Join(root, "iio:device0")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	for name, body := range map[string]string{
		"name":               "tsl2584tsv\n",
		"illuminance0_input": "57\n",
		"calibrated_lux":     "72\n",
	} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	iioGlob = filepath.Join(root, "*", "name")
	if alscal != "" {
		alscalPath = filepath.Join(root, "alscal")
		if err := os.WriteFile(alscalPath, []byte(alscal), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// A ams_0_0 baseline as the factory writes it, ahead of the 400 lux point.
const alscalWithBaseline = " ams_0_0=3,0,1 ams_400_0=272,316.667,35"

// The second-sourced batch: the 2540's name is listed with nothing behind
// it, and the 2584 answers on IIO.
func TestSecondSourceReadsIIO(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2540": false})
	dir := fakeIIO(t, alscalWithBaseline)
	got := Report()
	if got.Code != StatusOK || got.Path != filepath.Join(dir, "illuminance0_input") {
		t.Fatalf("got %+v, want ok at %s/illuminance0_input", got, dir)
	}
	var sawIIO bool
	for _, s := range got.Seen {
		sawIIO = sawIIO || s == "iio:tsl2584tsv"
	}
	if !sawIIO {
		t.Fatalf("Seen = %v, want the IIO device listed", got.Seen)
	}
	if v := Lux(); v == nil || *v != 57 {
		t.Fatalf("Lux() = %v, want 57 from illuminance0_input", v)
	}
}

// Without ams_0_0 every lit read leaks the copied string and logs two lines.
// An unreadable string cannot show it is there either.
func TestSecondSourceWithoutBaselineIsNotRead(t *testing.T) {
	for name, alscal := range map[string]string{
		"no ams_0_0":    " ams_400_0=272,316.667,35",
		"no idme value": "",
	} {
		t.Run(name, func(t *testing.T) {
			fakeBus(t, map[string]bool{"tsl2540": false})
			fakeIIO(t, alscal)
			got := Report()
			if got.Code != StatusUnsafe || got.Detail == "" {
				t.Fatalf("got %+v, want %q with a detail", got, StatusUnsafe)
			}
			if Present() || Lux() != nil {
				t.Fatal("a sensor that is unsafe to read must not be read")
			}
		})
	}
}

// Where both answer, the board's order decides: the 2540 first.
func TestFirstListedSensorWins(t *testing.T) {
	fakeBus(t, map[string]bool{"tsl2540": true})
	fakeIIO(t, alscalWithBaseline)
	if got := Report(); filepath.Base(got.Path) != "als_lux" {
		t.Fatalf("path = %q, want the tsl2540's als_lux", got.Path)
	}
}

// calibrated_lux kfree()s a pointer strsep() has moved, and the corruption
// surfaces later as an oops or a watchdog reset (#90). No firmware source may
// name it in code; comments explaining why are fine, so string literals are
// what is checked.
func TestNothingReadsCalibratedLux(t *testing.T) {
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "go.mod")); err != nil {
		t.Fatalf("%s is not the module root: %v", root, err)
	}
	var files int
	err = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(p, ".go") || strings.HasSuffix(p, "_test.go") {
			return err
		}
		f, err := parser.ParseFile(token.NewFileSet(), p, nil, parser.SkipObjectResolution)
		if err != nil {
			return err
		}
		files++
		ast.Inspect(f, func(n ast.Node) bool {
			if lit, ok := n.(*ast.BasicLit); ok && strings.Contains(lit.Value, "calibrated_lux") {
				t.Errorf("%s names calibrated_lux", p)
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if files < 50 {
		t.Fatalf("walked %d files under %s, expected the whole module", files, root)
	}
}

// From what the drivers print: a whole number (tsl2540 als_lux), or IIO's
// IIO_VAL_INT_PLUS_MICRO "%d.%06d" (tsl2572 in_illuminance0_input, read off
// 41C8 as "3.636000"), with the newline sysfs appends. Rounded to whole lux;
// 0 is a real reading (a covered sensor).
func TestParseLux(t *testing.T) {
	cases := []struct {
		in   string
		want int
		ok   bool
	}{
		{"309\n", 309, true},
		{"0\n", 0, true},
		{"3.636000\n", 4, true},
		{"56.973000", 57, true},
		{"0.499999", 0, true},
		{"0.500000", 1, true},
		{"0.000000", 0, true},
		{"123456789.999999", 123456790, true},
		{"-0.500000", 0, false}, // IIO prints negatives this way; not light
		{"-3", 0, false},
		{"NaN", 0, false},
		{"Inf", 0, false},
		{"1e3", 0, false},
		{"0x1p3", 0, false},
		{"1.", 0, false},
		{".5", 0, false},
		{"1,5", 0, false},
		{"", 0, false},
		{"\n", 0, false},
		{"1234567890", 0, false}, // past any sensor's range, and int32
	}
	for _, c := range cases {
		got, ok := parseLux(c.in)
		if ok != c.ok || got != c.want {
			t.Errorf("parseLux(%q) = %d, %v; want %d, %v", c.in, got, ok, c.want, c.ok)
		}
	}
}
